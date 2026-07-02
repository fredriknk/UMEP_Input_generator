import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from merge_footprint_weather import (
    Era5Row,
    Observation,
    default_output_path,
    derive_ustar_obukhov,
    exclude_incomplete_era5_years,
    incomplete_era5_years,
    merge_rows,
    parser,
    read_frost,
)


class MergeWeatherTests(unittest.TestCase):
    def setUp(self):
        self.timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.era = Era5Row(
            timestamp=self.timestamp,
            blh=500.0,
            sp=100000.0,
            t2m=280.0,
            d2m=275.0,
            iews=0.2,
            inss=0.1,
            ishf=0.0,
            moisture_flux=0.0,
            sdfor=10.0,
        )

    def test_neutral_flux_produces_finite_neutral_obukhov(self):
        ustar, obukhov = derive_ustar_obukhov(self.era)
        self.assertGreater(ustar, 0)
        self.assertEqual(obukhov, 1_000_000.0)

    def test_upward_heat_flux_produces_unstable_negative_obukhov(self):
        unstable = Era5Row(**{**self.era.__dict__, "ishf": -100.0})
        _, obukhov = derive_ustar_obukhov(unstable)
        self.assertLess(obukhov, 0)

    def test_frost_prefers_better_quality_before_source_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "frost.csv"
            path.write_text(
                "source_id,reference_time,element_id,value,unit,quality_code\n"
                "SN76914:0,2025-01-01T00:00:00Z,air_temperature,1,degC,1\n"
                "SN17850:0,2025-01-01T00:00:00Z,air_temperature,2,degC,0\n",
                encoding="utf-8",
            )
            result = read_frost(path, {self.timestamp})
        self.assertEqual(result[self.timestamp]["air_temperature"].value, 2.0)
        self.assertEqual(result[self.timestamp]["air_temperature"].source, "SN17850:0")

    def test_multiple_frost_files_share_quality_ranking(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.csv"
            second = Path(folder) / "second.csv"
            header = (
                "source_id,reference_time,element_id,value,unit,quality_code\n"
            )
            first.write_text(
                header
                + "SN76914:0,2025-01-01T00:00:00Z,air_temperature,1,degC,1\n",
                encoding="utf-8",
            )
            second.write_text(
                header
                + "SN17850:0,2025-01-01T00:00:00Z,air_temperature,2,degC,0\n",
                encoding="utf-8",
            )
            result = read_frost([first, second], {self.timestamp})
        observation = result[self.timestamp]["air_temperature"]
        self.assertEqual(observation.value, 2.0)
        self.assertEqual(observation.source, "SN17850:0")

    def test_frost_cli_accepts_multiple_and_repeated_arguments(self):
        args = parser().parse_args([
            "--frost", "one.csv", "two.csv",
            "--frost", "three.csv",
        ])
        self.assertEqual(
            args.frost,
            [Path("one.csv"), Path("two.csv"), Path("three.csv")],
        )

    def test_multiyear_default_output_and_completeness(self):
        second_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        second = Era5Row(
            **{**self.era.__dict__, "timestamp": second_timestamp}
        )
        rows = {self.timestamp: self.era, second_timestamp: second}
        self.assertEqual(
            default_output_path(rows),
            Path("local_weatherdata/merged_footprint_weather_2025-2026.csv"),
        )
        self.assertEqual(
            incomplete_era5_years(rows),
            [(2025, 1, 8760), (2026, 1, 8760)],
        )

    def test_incomplete_years_can_be_excluded(self):
        complete_timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        incomplete_timestamp = datetime(2023, 1, 1, tzinfo=timezone.utc)
        rows = {
            complete_timestamp: Era5Row(
                **{**self.era.__dict__, "timestamp": complete_timestamp}
            ),
            incomplete_timestamp: Era5Row(
                **{**self.era.__dict__, "timestamp": incomplete_timestamp}
            ),
        }
        filtered = exclude_incomplete_era5_years(
            rows, [(2023, 744, 8760)]
        )
        self.assertEqual(set(filtered), {complete_timestamp})

    def test_calm_local_wind_uses_era5_direction(self):
        frost = {
            self.timestamp: {
                "wind_speed": Observation(0.0, "m/s", "SN76914:0", "0"),
                "wind_from_direction": Observation(
                    123.0, "degrees", "SN76914:0", "0"
                ),
            }
        }
        rows = merge_rows(
            {self.timestamp: self.era},
            frost,
            {self.timestamp: (2.0, 250.0)},
        )
        self.assertEqual(rows[0]["wdir"], "250.0000")
        self.assertEqual(rows[0]["wind_speed"], "0.0000")
        self.assertEqual(
            rows[0]["wind_direction_source"], "era5:u10_v10:local_calm"
        )

    def test_netCDF_wind_is_used_when_frost_wind_is_missing(self):
        era_with_wind = Era5Row(**{**self.era.__dict__, "u10": -2.0, "v10": 0.0})
        rows = merge_rows({self.timestamp: era_with_wind}, {}, {})
        self.assertEqual(rows[0]["wdir"], "90.0000")
        self.assertEqual(rows[0]["wind_speed"], "2.0000")
        self.assertEqual(rows[0]["wind_direction_source"], "era5:u10_v10")


if __name__ == "__main__":
    unittest.main()
