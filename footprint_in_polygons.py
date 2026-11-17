#!/usr/bin/env python3
"""Share of a footprint that falls inside polygons, grouped by a class field.

Typical uses: the share of each tower's footprint inside its own field and the
neighbouring fields (class = field id), or the footprint-weighted share of soil
types or land-cover classes (class = soil or land-cover code).

Input footprints are the density rasters written by run_footprint_standalone.py
or run_tower_batch.py (footprint_*density.tif). Polygons can be a shapefile or a
GeoPackage layer; they are reprojected to the raster CRS when their CRS is known.
Where polygons overlap, each cell is counted once, for the first polygon in the
file. Shares are percentages of the footprint mass inside the raster, so check
captured_mass in footprint_placement_summary.csv: if it is well below 1, the grid
is too coarse or the fetch too short and the shares are less reliable.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import struct
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import rasterize
from rasterio.warp import transform_geom

OUTSIDE = "(outside polygons)"


def _parse_wkb(wkb: bytes) -> dict:
    """Decode a WKB Polygon or MultiPolygon (2D, optionally with Z/M) to a GeoJSON-like dict."""

    def ring_list(data: bytes, pos: int, order: str, dims: int) -> tuple[list, int]:
        (count,) = struct.unpack_from(order + "I", data, pos)
        pos += 4
        rings = []
        for _ in range(count):
            (points,) = struct.unpack_from(order + "I", data, pos)
            pos += 4
            ring = []
            for _ in range(points):
                ring.append(struct.unpack_from(order + "dd", data, pos))
                pos += 8 * dims
            rings.append(ring)
        return rings, pos

    def geometry(data: bytes, pos: int) -> tuple[dict, int]:
        order = "<" if data[pos] == 1 else ">"
        (code,) = struct.unpack_from(order + "I", data, pos + 1)
        pos += 5
        base = code % 1000
        dims = 2 + (code // 1000 in (1, 2)) + (code // 1000 == 3) * 2
        if base == 3:
            rings, pos = ring_list(data, pos, order, dims)
            return {"type": "Polygon", "coordinates": rings}, pos
        if base == 6:
            (count,) = struct.unpack_from(order + "I", data, pos)
            pos += 4
            polygons = []
            for _ in range(count):
                part, pos = geometry(data, pos)
                polygons.append(part["coordinates"])
            return {"type": "MultiPolygon", "coordinates": polygons}, pos
        raise ValueError(f"Unsupported geometry type {code}; only polygons are supported")

    return geometry(wkb, 0)[0]


def _gpkg_geometry(blob: bytes) -> dict:
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    return _parse_wkb(blob[8 + envelope:])


def read_polygons(path: Path, class_field: str | None, layer: str | None = None) -> tuple[list[tuple[dict, str]], CRS | None]:
    """Return [(geometry, class)] and the polygons' CRS (None if unknown)."""
    if path.suffix.lower() == ".gpkg":
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as con:
            layers = con.execute(
                "select table_name, column_name, srs_id from gpkg_geometry_columns").fetchall()
            if not layers:
                raise ValueError(f"No vector layers in {path}")
            match = [row for row in layers if layer is None or row[0] == layer]
            if not match:
                raise ValueError(f"Layer {layer!r} not found; available: {[row[0] for row in layers]}")
            table, geom_column, srs = match[0]
            columns = [row[1] for row in con.execute(f'pragma table_info("{table}")')]
            field = class_field or next(c for c in columns if c not in (geom_column, "fid"))
            if field not in columns:
                raise ValueError(f"Field {field!r} not in layer {table!r}; available: {columns}")
            rows = con.execute(f'select "{geom_column}", "{field}" from "{table}"').fetchall()
            org = con.execute(
                "select organization, organization_coordsys_id from gpkg_spatial_ref_sys where srs_id = ?",
                (srs,)).fetchone()
        polygons = [(_gpkg_geometry(g), str(v)) for g, v in rows if g]
        crs = CRS.from_epsg(org[1]) if org and str(org[0]).upper() == "EPSG" and org[1] > 0 else None
        return polygons, crs

    import shapefile

    reader = shapefile.Reader(str(path))
    names = [f[0] for f in reader.fields[1:]]
    field = class_field or names[0]
    if field not in names:
        raise ValueError(f"Field {field!r} not in {path.name}; available: {names}")
    polygons = []
    # Read shape i with attribute row i. Empty shapes and deleted attribute rows
    # are skipped (pairing them with zip() would misalign files that contain both).
    for index in range(len(reader)):
        shape = reader.shape(index)
        if shape.shapeType == shapefile.NULL:
            continue
        record = reader.record(index)
        if record is None:
            continue
        polygons.append((shape.__geo_interface__, str(record[field])))
    prj = path.with_suffix(".prj")
    crs = CRS.from_wkt(prj.read_text(encoding="utf-8")) if prj.exists() else None
    return polygons, crs


def shares(raster: Path, polygons: list[tuple[dict, str]], polygon_crs: CRS | None) -> dict[str, float]:
    """Percent of the footprint density in each class (and outside all polygons)."""
    with rasterio.open(raster) as src:
        density = src.read(1, masked=True).filled(0).astype(float)
        total = density.sum()
        if total <= 0:
            raise ValueError(f"{raster} contains no footprint mass")
        result: dict[str, float] = defaultdict(float)
        covered = np.zeros(density.shape, dtype=bool)
        for geom, label in polygons:
            if polygon_crs is not None and src.crs is not None and polygon_crs != src.crs:
                geom = transform_geom(polygon_crs, src.crs, geom)
            mask = rasterize([(geom, 1)], out_shape=density.shape, transform=src.transform,
                             fill=0, dtype="uint8").astype(bool) & ~covered
            covered |= mask
            result[label] += 100.0 * density[mask].sum() / total
        result[OUTSIDE] += 100.0 * density[~covered].sum() / total
    return dict(result)


def find_rasters(paths: list[Path]) -> list[Path]:
    """Expand directories to the density rasters they contain (interpolated ones preferred)."""
    found = []
    for path in paths:
        if path.is_dir():
            rasters = sorted(path.rglob("*density.tif"))
            names = {r.name for r in rasters}
            found += [r for r in rasters
                      if "interpolated" in r.name or r.name.replace("_density", "_interpolated_density") not in names]
        else:
            found.append(path)
    return found


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    result.add_argument("footprints", nargs="+", type=Path,
                        help="Density GeoTIFFs, or folders searched for *density.tif")
    result.add_argument("--polygons", type=Path, required=True, help="Shapefile or GeoPackage")
    result.add_argument("--layer", help="GeoPackage layer (default: first layer)")
    result.add_argument("--class-field", help="Attribute that names each class (default: first attribute)")
    result.add_argument("--output", type=Path, required=True, help="CSV: raster, class, percent")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        polygons, crs = read_polygons(args.polygons, args.class_field, args.layer)
        if not polygons:
            raise ValueError(f"No polygons could be read from {args.polygons}")
        rasters = find_rasters(args.footprints)
        if not rasters:
            raise ValueError("No footprint density rasters found")
        if crs is None:
            print("Warning: polygon CRS unknown; assuming it matches the rasters", file=sys.stderr)
        rows = []
        for raster in rasters:
            for label, pct in sorted(shares(raster, polygons, crs).items(), key=lambda kv: -kv[1]):
                rows.append({"raster": raster.as_posix(), "class": label, "percent": round(pct, 2)})
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["raster", "class", "percent"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows for {len(rasters)} raster(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
