import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from run_footprint_standalone import (
    Grid,
    contribution_percent_raster,
    footprint_for_row,
    validate_row,
    write_qgis_styles,
    _select_output_paths,
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

    def test_writes_qgis_styles_for_both_rasters(self):
        with tempfile.TemporaryDirectory() as folder:
            density = Path(folder) / "footprint_density.tif"
            percent = Path(folder) / "footprint_percent.tif"
            density_qml, percent_qml = write_qgis_styles(density, percent, 0.01)
            self.assertIn("singlebandpseudocolor", density_qml.read_text())
            self.assertIn("#d7191c", percent_qml.read_text())
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


if __name__ == "__main__":
    unittest.main()
