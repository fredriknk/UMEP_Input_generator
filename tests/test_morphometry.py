import tempfile
import unittest
from pathlib import Path

import numpy as np

from calculate_morphometry import Tower, directional_morphometry, kanda, read_towers, write_morphology
from generate_umep_footprint_input import read_morphology


class MorphometryTests(unittest.TestCase):
    def test_kanda_empty_sector_is_zero(self):
        self.assertEqual(kanda(0, 0, 0, 0, 0), (0.0, 0.0))

    def test_directional_result_has_umep_shape(self):
        buildings = np.zeros((40, 40))
        buildings[4:12, 19:22] = 8.0
        result = directional_morphometry(buildings, 1.0, 45.0)
        self.assertEqual(result.shape, (8, 8))
        self.assertTrue((result[:, 0] == np.arange(0, 360, 45)).all())
        self.assertGreater(result[:, 2].max(), 0)
        self.assertGreater(result[:, 7].max(), 0)

    def test_output_is_readable_by_generator(self):
        values = np.array([[0, 0.1, 0.1, 5, 6, 1, 2, 0.5]])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "morphology.txt"
            write_morphology(path, values)
            morphology = read_morphology(path)
        self.assertAlmostEqual(morphology[0].z0, 0.5)
        self.assertAlmostEqual(morphology[0].zd, 2.0)

    def test_tower_csv_can_override_measurement_height(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "towers.csv"
            path.write_text("id,x,y,measurement_height_m\nA,10,20,3.5\n", encoding="utf-8")
            towers = read_towers(path)
        self.assertEqual(towers, [Tower("A", 10.0, 20.0, 3.5)])

    def test_missing_tower_file_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "tower file not found"):
            read_towers(Path("definitely_missing_towers.csv"))


if __name__ == "__main__":
    unittest.main()
