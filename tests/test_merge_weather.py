import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from merge_footprint_weather import (
    Era5Row,
    Observation,
    derive_ustar_obukhov,
    merge_rows,
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
