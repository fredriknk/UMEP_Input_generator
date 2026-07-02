import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from run_footprint_standalone import (
    Grid,
    contribution_percent_raster,
    footprint_for_row,
    interpolate_footprint,
    validate_row,
    write_contours,
    write_qgis_styles,
    _select_output_paths,
    _placement_labels,
    parse_months,
)


def sample_row():
    return np.array([
        2025, 1, 0, 0, 0.01, 0.0, 2.0, 1.2783,
        11480.4881, 0.6392, 1.0, 637.951, 60.0,
    ])


class StandaloneFootprintTests(unittest.TestCase):
    def test_footprint_is_upwind_and_has_positive_mass(self):
        grid = Grid(fetch=200.0, resolution=5.0)
        x, y = grid.coordinates()
        footprint = footprint_for_row(sample_row(), x, y)
        self.assertEqual(footprint.shape, (80, 80))
        self.assertTrue(np.isfinite(footprint).all())
        self.assertGreater(footprint.sum(), 0)
        # Wind from north: source contribution lies north/upwind of the tower.
        self.assertGreater(footprint[y > 0].sum(), footprint[y < 0].sum())

    def test_percent_raster_and_mass(self):
        footprint = np.array([[4.0, 2.0], [1.0, 0.0]])
        percent, mass = contribution_percent_raster(footprint, 1.0)
        self.assertEqual(mass, 7.0)
        self.assertLess(percent[0, 0], percent[1, 0])
        self.assertEqual(percent[1, 1], 0)

    def test_validation_rejects_low_ustar(self):
        row = sample_row()
        row[9] = 0.1
        self.assertEqual(validate_row(row), "ustar_not_above_0.1")

    def test_validation_rejects_roughness_sublayer(self):
        row = sample_row()
        row[4] = 0.2
        self.assertEqual(validate_row(row), "sensor_below_roughness_sublayer")

    def test_non_divisible_grid_is_symmetric(self):
        grid = Grid(fetch=10.0, resolution=6.0)
        x, y = grid.coordinates()
        self.assertEqual(grid.size, 4)
        self.assertEqual(grid.extent, 12.0)
        self.assertAlmostEqual(float(x.min()), -9.0)
        self.assertAlmostEqual(float(x.max()), 9.0)

    def test_interpolation_is_finer_and_preserves_mass(self):
        grid = Grid(fetch=10.0, resolution=5.0)
        source = np.zeros((grid.size, grid.size))
        source[1:3, 1:3] = 1.0
        interpolated, fine_grid = interpolate_footprint(source, grid, 2.0, 0.5)
        self.assertEqual(interpolated.shape, (10, 10))
        self.assertEqual(fine_grid.resolution, 2.0)
        self.assertAlmostEqual(
            float(source.sum()) * grid.resolution**2,
            float(interpolated.sum()) * fine_grid.resolution**2,
        )

    def test_writes_labelled_contour_shapefile_and_qml(self):
        grid = Grid(fetch=10.0, resolution=1.0)
        x, y = grid.coordinates()
        percent = np.clip(np.ceil(np.hypot(x, y) * 10), 1, 100).astype(np.uint8)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "footprint_contours.shp"
            shape_path, qml_path = write_contours(
                path, percent, grid, 100.0, 200.0, "EPSG:25832", (20, 40, 60)
            )
            import shapefile

            reader = shapefile.Reader(str(shape_path))
            levels = {record[0] for record in reader.records()}
            reader.close()
            self.assertEqual(levels, {20, 40, 60})
            qml = qml_path.read_text(encoding="utf-8")
            self.assertIn('fieldName="level"', qml)
            self.assertIn("<text-buffer ", qml)
            self.assertIn('<placement placement="3"', qml)
            self.assertIn('placementFlags="9"', qml)
            self.assertIn('repeatDistance="0"', qml)

    def test_writes_qgis_styles_for_both_rasters(self):
        with tempfile.TemporaryDirectory() as folder:
            density = Path(folder) / "footprint_density.tif"
            percent = Path(folder) / "footprint_percent.tif"
            density_qml, percent_qml = write_qgis_styles(density, percent, 0.01)
            self.assertIn("singlebandpseudocolor", density_qml.read_text())
            self.assertIn("#d7191c", percent_qml.read_text())
            self.assertIn('min="81" max="100"', percent_qml.read_text())
            self.assertEqual(density_qml.name, "footprint_density.qml")

    def test_selects_normal_output_names_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            density, percent = _select_output_paths(Path(folder) / "footprint")
            self.assertEqual(density.name, "footprint_density.tif")
            self.assertEqual(percent.name, "footprint_percent.tif")

    def test_locked_output_selects_matched_run_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / "footprint"
            with patch("run_footprint_standalone._can_replace", return_value=False):
                density, percent = _select_output_paths(prefix)
            self.assertEqual(density.name, "footprint_run2_density.tif")
            self.assertEqual(percent.name, "footprint_run2_percent.tif")

    def test_placement_labels(self):
        row = sample_row()
        row[1] = 180
        row[8] = -20
        row[10] = 46
        self.assertEqual(
            _placement_labels(row, frozenset(range(4, 11))),
            ("growing", "stability_unstable", "wind_NE"),
        )

    def test_parse_growing_months(self):
        self.assertEqual(parse_months("4-6,9"), (4, 5, 6, 9))


if __name__ == "__main__":
    unittest.main()
