import argparse
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from generate_umep_footprint_input import (
    CropHeightPoint,
    Morphology,
    OutputRow,
    interpolate_crop_height,
    interpolate_morphology,
    interpolate_morphology_with_crop,
    parse_timestamp,
    read_crop_height_schedule,
    read_morphology,
    validate_rows,
    wind_direction,
)


class GeneratorTests(unittest.TestCase):
    def test_reads_umep_morphology_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "morph.txt"
            path.write_text(
                " Wd pai fai zH zHmax zHstd zd z0\n"
                " 0 .2 .3 8 10 2 4 1\n"
                " 90 .2 .3 8 10 2 8 2\n",
                encoding="utf-8",
            )
            values = read_morphology(path)
        self.assertEqual(values[1], Morphology(90.0, 2.0, 8.0))

    def test_reads_zero_object_morphology_sector(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "morph.txt"
            path.write_text(
                "Wd zd z0\n"
                "0 0 0\n"
                "5 2.27 .324\n",
                encoding="utf-8",
            )
            values = read_morphology(path)
        self.assertEqual(values[0], Morphology(0.0, 0.0, 0.0))

    def test_reads_and_interpolates_crop_height_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "crop.csv"
            path.write_text(
                "date,height_m\n"
                "2025-05-01,0.0\n"
                "2025-05-11,0.4\n",
                encoding="utf-8",
            )
            schedule = read_crop_height_schedule(path)
        self.assertEqual(
            schedule,
            [
                CropHeightPoint(date(2025, 5, 1), 0.0),
                CropHeightPoint(date(2025, 5, 11), 0.4),
            ],
        )
        self.assertAlmostEqual(
            interpolate_crop_height(schedule, datetime(2025, 5, 6, 12)), 0.2
        )

    def test_crop_fills_only_zero_object_sectors(self):
        morphology = [
            Morphology(0, 0, 0),
            Morphology(90, 0.324, 2.27),
        ]
        field = interpolate_morphology_with_crop(morphology, 0, 0.6)
        building = interpolate_morphology_with_crop(morphology, 90, 0.6)
        self.assertAlmostEqual(field.z0, 0.0738)
        self.assertAlmostEqual(field.zd, 0.4)
        self.assertEqual(building, Morphology(90, 0.324, 2.27))

    def test_interpolates_across_north(self):
        values = [Morphology(10, 2, 4), Morphology(350, 1, 2)]
        result = interpolate_morphology(sorted(values, key=lambda x: x.direction), 0)
        self.assertAlmostEqual(result.z0, 1.5)
        self.assertAlmostEqual(result.zd, 3.0)

    def test_derives_meteorological_wind_direction(self):
        self.assertAlmostEqual(wind_direction({"u10": "-1", "v10": "0"}), 90)
        self.assertAlmostEqual(wind_direction({"u10": "0", "v10": "-1"}), 0)

    def test_normalizes_explicit_utc_timestamp_to_naive_utc(self):
        self.assertEqual(
            parse_timestamp({"valid_time": "2025-01-01T01:00:00+01:00"}),
            datetime(2025, 1, 1, 0, 0),
        )

    def test_rejects_sensor_below_displacement_height(self):
        row = OutputRow(
            datetime(2025, 1, 1),
            z0=1.0,
            zd=12.0,
            measurement_height=2.0,
            sigv=1.0,
            obukhov=1000.0,
            ustar=0.5,
            wind_direction=0.0,
            boundary_layer_height=1000.0,
            porosity=60.0,
        )
        errors = validate_rows([row], "kljun")
        self.assertIn("not above displacement height", errors[0])

    def test_accepts_valid_kljun_row(self):
        row = OutputRow(
            datetime(2025, 1, 1),
            z0=1.0,
            zd=10.0,
            measurement_height=30.0,
            sigv=1.0,
            obukhov=1000.0,
            ustar=0.5,
            wind_direction=0.0,
            boundary_layer_height=1000.0,
            porosity=60.0,
        )
        self.assertEqual(validate_rows([row], "kljun"), [])


if __name__ == "__main__":
    unittest.main()
