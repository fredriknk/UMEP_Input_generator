import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
import shapefile
from rasterio.transform import from_origin

from footprint_in_polygons import OUTSIDE, find_rasters, read_polygons, shares


def write_raster(path: Path, data: np.ndarray) -> None:
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype="float64", crs="EPSG:25832", transform=from_origin(0, 100, 10, 10)) as dst:
        dst.write(data, 1)


def square(x0, y0, x1, y1):
    return [[(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]]


class PolygonShareTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dir = Path(self.folder.name)
        # 10 x 10 grid of 10 m cells covering x 0-100, y 0-100; mass only in the left half
        data = np.zeros((10, 10))
        data[:, :5] = 1.0
        self.raster = self.dir / "footprint_density.tif"
        write_raster(self.raster, data)

    def tearDown(self):
        self.folder.cleanup()

    def write_shapefile(self, name="fields", with_null=False):
        path = self.dir / f"{name}.shp"
        writer = shapefile.Writer(str(path), shapeType=shapefile.POLYGON)
        writer.field("field_id", "C")
        if with_null:
            writer.null()
            writer.record("deleted")
        writer.poly(square(0, 0, 30, 100))
        writer.record("A")
        writer.poly(square(30, 0, 100, 100))
        writer.record("B")
        writer.close()
        path.with_suffix(".prj").write_text(rasterio.crs.CRS.from_epsg(25832).to_wkt(), encoding="utf-8")
        return path

    def test_shares_by_class_from_shapefile(self):
        polygons, crs = read_polygons(self.write_shapefile(), "field_id")
        result = shares(self.raster, polygons, crs)
        self.assertAlmostEqual(result["A"], 60.0)
        self.assertAlmostEqual(result["B"], 40.0)
        self.assertAlmostEqual(result[OUTSIDE], 0.0)

    def test_null_shapes_are_skipped(self):
        polygons, _ = read_polygons(self.write_shapefile("with_null", with_null=True), "field_id")
        self.assertEqual([label for _, label in polygons], ["A", "B"])

    def test_mass_outside_polygons_is_reported(self):
        polygons = [({"type": "Polygon", "coordinates": square(0, 0, 20, 100)}, "A")]
        result = shares(self.raster, polygons, None)
        self.assertAlmostEqual(result["A"], 40.0)
        self.assertAlmostEqual(result[OUTSIDE], 60.0)

    def test_overlapping_polygons_count_each_cell_once(self):
        polygons = [({"type": "Polygon", "coordinates": square(0, 0, 50, 100)}, "A"),
                    ({"type": "Polygon", "coordinates": square(0, 0, 50, 100)}, "B")]
        result = shares(self.raster, polygons, None)
        self.assertAlmostEqual(result["A"], 100.0)
        self.assertAlmostEqual(result.get("B", 0.0), 0.0)

    def test_reads_geopackage_layer(self):
        path = self.dir / "fields.gpkg"
        con = sqlite3.connect(path)
        con.executescript(
            "create table gpkg_spatial_ref_sys (srs_name text, srs_id integer, organization text,"
            " organization_coordsys_id integer, definition text);"
            "insert into gpkg_spatial_ref_sys values ('ETRS89 / UTM 32N', 25832, 'EPSG', 25832, '');"
            "create table gpkg_geometry_columns (table_name text, column_name text,"
            " geometry_type_name text, srs_id integer, z integer, m integer);"
            "insert into gpkg_geometry_columns values ('fields', 'geom', 'POLYGON', 25832, 0, 0);"
            "create table fields (fid integer primary key, geom blob, soil text);")
        ring = square(0, 0, 50, 100)[0]
        wkb = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring)) + b"".join(
            struct.pack("<dd", *p) for p in ring)
        blob = b"GP" + bytes([0, 1]) + struct.pack("<i", 25832) + wkb  # no envelope, little-endian
        con.execute("insert into fields (geom, soil) values (?, ?)", (blob, "Stagnosol"))
        con.commit()
        con.close()
        polygons, crs = read_polygons(path, "soil", "fields")
        self.assertEqual(crs, rasterio.crs.CRS.from_epsg(25832))
        self.assertAlmostEqual(shares(self.raster, polygons, crs)["Stagnosol"], 100.0)

    def test_find_rasters_prefers_interpolated(self):
        folder = self.dir / "tower"
        folder.mkdir()
        for name in ("footprint_density.tif", "footprint_interpolated_density.tif",
                     "footprint_growing_density.tif"):
            (folder / name).write_bytes(b"")
        names = sorted(p.name for p in find_rasters([self.dir / "tower"]))
        self.assertEqual(names, ["footprint_growing_density.tif", "footprint_interpolated_density.tif"])


if __name__ == "__main__":
    unittest.main()
