#!/usr/bin/env python
"""Calculate UMEP-compatible directional morphometry around flux towers."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Tower:
    name: str
    x: float
    y: float
    measurement_height: float | None = None
    crs_wkt: str | None = None


def _field(fields: dict[str, str], *names: str) -> str | None:
    return next((fields[name] for name in names if name in fields), None)


def read_towers(path: Path) -> list[Tower]:
    """Read id/x/y[/measurement_height_m] CSV or Point shapefile."""
    if not path.exists():
        raise ValueError(f"tower file not found: {path}")
    if path.suffix.lower() == ".shp":
        return _read_point_shapefile(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header")
        fields = {value.strip().lower(): value for value in reader.fieldnames}
        x_key = _field(fields, "x", "easting", "tower_x")
        y_key = _field(fields, "y", "northing", "tower_y")
        id_key = _field(fields, "id", "name", "tower_id")
        height_key = _field(fields, "measurement_height_m", "measurement_height", "zm")
        if not x_key or not y_key:
            raise ValueError("tower CSV must contain x/easting and y/northing columns")
        towers = []
        for number, row in enumerate(reader, start=1):
            height = float(row[height_key]) if height_key and row[height_key].strip() else None
            towers.append(Tower(row[id_key].strip() if id_key else f"tower_{number}", float(row[x_key]), float(row[y_key]), height))
        if not towers:
            raise ValueError(f"no tower rows found in {path}")
        return towers


def _read_point_shapefile(path: Path) -> list[Tower]:
    """Read Point geometry, attributes, and CRS from a conventional shapefile."""
    try:
        import shapefile
    except ImportError as exc:
        raise RuntimeError("pyshp is required for shapefiles; install requirements.txt") from exc
    prj_path = path.with_suffix(".prj")
    crs_wkt = prj_path.read_text(encoding="utf-8").strip() if prj_path.exists() else None
    try:
        reader = shapefile.Reader(str(path))
    except shapefile.ShapefileException as exc:
        raise ValueError(
            f"could not open tower shapefile {path}; check its .shp/.shx/.dbf sidecars"
        ) from exc
    field_names = [field[0] for field in reader.fields[1:]]
    lower = {name.lower(): name for name in field_names}
    id_key = _field(lower, "id", "name", "tower_id")
    height_key = _field(lower, "measurement_height_m", "measurement_height", "zm")
    towers: list[Tower] = []
    for number, shape_record in enumerate(reader.iterShapeRecords(), start=1):
        shape = shape_record.shape
        if shape.shapeType != shapefile.POINT:
            raise ValueError("only Point shapefiles are supported; use a CSV for other geometry types")
        attributes = shape_record.record.as_dict()
        name_value = attributes.get(id_key) if id_key else None
        height_value = attributes.get(height_key) if height_key else None
        towers.append(
            Tower(
                str(name_value) if name_value not in (None, "") else f"tower_{number}",
                float(shape.points[0][0]),
                float(shape.points[0][1]),
                float(height_value) if height_value not in (None, "") else None,
                crs_wkt,
            )
        )
    if not towers:
        raise ValueError(f"no points found in {path}")
    return towers


def project_tower(tower: Tower, target_crs: str) -> Tower:
    """Transform a shapefile tower to the raster CRS; CSV coordinates are assumed ready."""
    if not tower.crs_wkt:
        return tower
    from rasterio.crs import CRS
    from rasterio.warp import transform

    source = CRS.from_wkt(tower.crs_wkt)
    target = CRS.from_user_input(target_crs)
    if source == target:
        return Tower(tower.name, tower.x, tower.y, tower.measurement_height, target.to_wkt())
    xs, ys = transform(source, target, [tower.x], [tower.y])
    return Tower(tower.name, xs[0], ys[0], tower.measurement_height, target.to_wkt())


def kanda(z_h: float, fai: float, pai: float, z_max: float, z_sd: float) -> tuple[float, float]:
    """Return displacement height and roughness length using UMEP's Kanda method 1."""
    if z_h <= 0 or z_max <= 0 or pai <= 0 or fai <= 0:
        return 0.0, 0.0
    alpha, kappa = 4.43, 0.4
    zd_mac = (1.0 + alpha ** (-pai) * (pai - 1.0)) * z_h
    term = 0.5 * (1.2 / kappa**2) * (1.0 - zd_mac / z_h) * fai
    z0_mac = 0.0 if term <= 0 else z_h * (1.0 - zd_mac / z_h) * math.exp(-(term**-0.5))
    x_value = (z_sd + z_h) / z_max
    if 0 < x_value <= 1:
        zd = (-0.17 * x_value**2 + (1.29 * pai**0.36 + 0.17) * x_value) * z_max
    else:
        zd = 1.29 * pai**0.36 * z_h
    y_value = pai * z_sd / z_h
    z0 = (20.21 * y_value**2 - 0.77 * y_value + 0.71) * z0_mac
    return max(0.0, float(zd)), max(0.0, float(z0))


def directional_morphometry(buildings: np.ndarray, pixel_size: float, angle_step: float = 5.0) -> np.ndarray:
    """Reproduce the UMEP point calculator's upwind centre-ray calculation."""
    try:
        from scipy.ndimage import rotate
    except ImportError as exc:
        raise RuntimeError("scipy is required for morphometry; install requirements.txt") from exc
    if buildings.ndim != 2 or min(buildings.shape) < 3:
        raise ValueError("morphometry raster subset is too small")
    # UMEP expects a square and samples the first half of its centre column.
    n = min(buildings.shape)
    buildings = buildings[:n, :n].astype(np.float64, copy=True)
    buildings[~np.isfinite(buildings) | (buildings < 2.0)] = 0.0
    centre, ray_length = n // 2, n // 2
    results = []
    for angle in np.arange(0.0, 360.0, angle_step):
        rotated = rotate(buildings, angle, order=0, reshape=False, mode="nearest", prefilter=False)
        ray = rotated[:ray_length, centre]
        occupied = ray[ray > 2.0]
        walls = np.diff(rotated[:, centre])[:ray_length]
        walls = walls[walls > 2.0]
        pai = occupied.size / ray_length
        fai = float(walls.sum()) / (ray_length * pixel_size)
        if occupied.size:
            z_h, z_max, z_sd = float(occupied.mean()), float(occupied.max()), float(occupied.std())
        else:
            z_h = z_max = z_sd = 0.0
        zd, z0 = kanda(z_h, fai, pai, z_max, z_sd)
        results.append((angle, pai, fai, z_h, z_max, z_sd, zd, z0))
    return np.asarray(results)


def calculate_for_tower(dom_path: Path, dtm_path: Path, tower: Tower, radius: float, angle_step: float) -> tuple[np.ndarray, str]:
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(dom_path) as dom, rasterio.open(dtm_path) as dtm:
        if dom.crs != dtm.crs or dom.transform != dtm.transform or dom.shape != dtm.shape:
            raise ValueError("DOM and DTM must have identical CRS, transform, and dimensions")
        if dom.crs is None or not dom.crs.is_projected:
            raise ValueError("DOM/DTM must use a projected CRS with metre-scale coordinates")
        tower = project_tower(tower, dom.crs.to_string())
        row, col = dom.index(tower.x, tower.y)
        pixel_width, pixel_height = abs(dom.transform.a), abs(dom.transform.e)
        if not math.isclose(pixel_width, pixel_height, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("DOM/DTM pixels must be square for directional morphometry")
        pixel_size = pixel_width
        half = int(math.ceil(radius / pixel_size))
        window = Window(col - half, row - half, 2 * half, 2 * half)
        if window.col_off < 0 or window.row_off < 0 or window.col_off + window.width > dom.width or window.row_off + window.height > dom.height:
            raise ValueError(f"{tower.name}: {radius:g} m search area extends outside the rasters")
        surface = dom.read(1, window=window, masked=True).filled(np.nan)
        terrain = dtm.read(1, window=window, masked=True).filled(np.nan)
        if not np.isfinite(surface).all() or not np.isfinite(terrain).all():
            raise ValueError(f"{tower.name}: search area contains missing raster cells")
        return directional_morphometry(surface - terrain, pixel_size, angle_step), dom.crs.to_string()


def write_morphology(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(path, values, fmt=("%.1f", "%.6f", "%.6f", "%.4f", "%.4f", "%.4f", "%.4f", "%.4f"),
               header="Wd pai fai zH zHmax zHstd zd z0", comments="")


def safe_name(value: str) -> str:
    result = "".join(character if character.isalnum() or character in "-_" else "_" for character in value.strip())
    return result or "tower"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Calculate UMEP directional Kanda morphometry for flux-tower points.")
    result.add_argument("--dom", type=Path, required=True)
    result.add_argument("--dtm", type=Path, required=True)
    result.add_argument("--towers", type=Path, required=True, help="Point SHP or CSV with id,x,y")
    result.add_argument("--output-dir", type=Path, default=Path("output/towers"))
    result.add_argument("--radius", type=float, default=200.0, help="Morphometry search radius in metres")
    result.add_argument("--angle-step", type=float, default=5.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if (
        args.radius <= 0
        or args.angle_step <= 0
        or not math.isclose(360 / args.angle_step, round(360 / args.angle_step))
    ):
        print("error: radius must be positive and angle-step must divide 360", file=sys.stderr)
        return 2
    try:
        towers = read_towers(args.towers)
        for tower in towers:
            values, crs = calculate_for_tower(args.dom, args.dtm, tower, args.radius, args.angle_step)
            output = args.output_dir / safe_name(tower.name) / "morphology.txt"
            write_morphology(output, values)
            print(f"{tower.name}: {output} ({crs})")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
