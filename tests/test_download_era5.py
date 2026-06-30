import unittest

from download_era5_footprint_data import (
    VARIABLES,
    build_request,
    monthly_output_path,
    nearest_grid_point,
)
from pathlib import Path


class Era5DownloadTests(unittest.TestCase):
    def test_selects_existing_nearest_era5_grid_point(self):
        self.assertEqual(nearest_grid_point(59.66024225482937), 59.75)
        self.assertEqual(nearest_grid_point(10.78266480752292), 10.75)

    def test_builds_monthly_point_request(self):
        request = build_request(2025, 2, 59.66024225482937, 10.78266480752292)
        self.assertEqual(request["year"], ["2025"])
        self.assertEqual(request["area"], [59.75, 10.75, 59.75, 10.75])
        self.assertEqual(request["month"], ["02"])
        self.assertEqual(len(request["day"]), 28)
        self.assertEqual(len(request["time"]), 24)
        self.assertEqual(request["variable"], VARIABLES)
        self.assertIn("boundary_layer_height", request["variable"])
        self.assertIn(
            "instantaneous_eastward_turbulent_surface_stress",
            request["variable"],
        )

    def test_adds_month_suffix_for_full_year_download(self):
        base = Path("era5_footprint_parameters_2025.nc")
        self.assertEqual(
            monthly_output_path(base, 7, False),
            Path("era5_footprint_parameters_2025_07.nc"),
        )
        self.assertEqual(monthly_output_path(base, 7, True), base)


if __name__ == "__main__":
    unittest.main()
