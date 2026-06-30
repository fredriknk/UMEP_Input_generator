#!/usr/bin/env python3
"""Download ERA5 fields needed to derive UMEP footprint parameters."""

from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
from pathlib import Path


DATASET = "reanalysis-era5-single-levels"
VARIABLES = [
    "boundary_layer_height",
    "surface_pressure",
    "2m_temperature",
    "2m_dewpoint_temperature",
    "instantaneous_eastward_turbulent_surface_stress",
    "instantaneous_northward_turbulent_surface_stress",
    "instantaneous_surface_sensible_heat_flux",
    "instantaneous_moisture_flux",
    "standard_deviation_of_filtered_subgrid_orography",
]


def nearest_grid_point(value: float, resolution: float = 0.25) -> float:
    """Return the nearest regular ERA5 latitude/longitude grid coordinate."""
    scaled = value / resolution
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded * resolution


def build_request(
    year: int, month: int, latitude: float, longitude: float
) -> dict:
    grid_latitude = nearest_grid_point(latitude)
    grid_longitude = nearest_grid_point(longitude)
    days_in_month = calendar.monthrange(year, month)[1]
    return {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": [f"{day:02d}" for day in range(1, days_in_month + 1)],
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "data_format": "netcdf",
        "download_format": "unarchived",
        # CDS area order is north, west, south, east.
        "area": [
            grid_latitude,
            grid_longitude,
            grid_latitude,
            grid_longitude,
        ],
    }


def monthly_output_path(base: Path, month: int, single_month: bool) -> Path:
    if single_month:
        return base
    return base.with_name(f"{base.stem}_{month:02d}{base.suffix}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Download hourly ERA5 fields needed to derive boundary-layer height, "
            "friction velocity, and Obukhov length for UMEP."
        )
    )
    result.add_argument("--year", type=int, default=2025)
    result.add_argument(
        "--month",
        type=int,
        help="Download only this month (1-12); default downloads all months separately",
    )
    result.add_argument("--latitude", type=float, default=59.66024225482937)
    result.add_argument("--longitude", type=float, default=10.78266480752292)
    result.add_argument(
        "--output",
        type=Path,
        help=(
            "Output NetCDF path (default: "
            "era_5_weatherdata/era5_footprint_parameters_YEAR.nc)"
        ),
    )
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="Print monthly CDS request(s) without accessing credentials or downloading",
    )
    result.add_argument(
        "--overwrite",
        action="store_true",
        help="Download again when an output file already exists",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 1940 <= args.year <= 2100:
        print("error: year must be between 1940 and 2100", file=sys.stderr)
        return 2
    if not -90 <= args.latitude <= 90:
        print("error: latitude must be between -90 and 90", file=sys.stderr)
        return 2
    if not -180 <= args.longitude <= 180:
        print("error: longitude must be between -180 and 180", file=sys.stderr)
        return 2
    if args.month is not None and not 1 <= args.month <= 12:
        print("error: month must be between 1 and 12", file=sys.stderr)
        return 2

    base_output = args.output or Path(
        f"era_5_weatherdata/era5_footprint_parameters_{args.year}.nc"
    )
    months = [args.month] if args.month is not None else list(range(1, 13))
    requests = [
        (
            month,
            build_request(args.year, month, args.latitude, args.longitude),
            monthly_output_path(base_output, month, args.month is not None),
        )
        for month in months
    ]
    grid_latitude, grid_longitude = requests[0][1]["area"][:2]

    if args.dry_run:
        print(f"Dataset: {DATASET}")
        print(f"Nearest ERA5 grid point: {grid_latitude}, {grid_longitude}")
        for month, request, output in requests:
            print(f"Month {month:02d} output: {output}")
            print(json.dumps(request, indent=2))
        return 0

    credentials = Path.home() / ".cdsapirc"
    if not credentials.is_file():
        print(
            f"error: CDS credentials not found at {credentials}. "
            "Create this file from https://cds.climate.copernicus.eu/how-to-api",
            file=sys.stderr,
        )
        return 2

    try:
        import cdsapi
    except ImportError:
        print(
            'error: cdsapi is not installed; run: python -m pip install "cdsapi>=0.7.7"',
            file=sys.stderr,
        )
        return 2

    client = cdsapi.Client()
    print(
        f"Requesting {args.year} in {len(requests)} monthly chunk(s) at "
        f"ERA5 grid point {grid_latitude}, {grid_longitude}"
    )
    downloaded = 0
    skipped = 0
    for month, request, output in requests:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and not args.overwrite:
            print(f"Skipping existing month {month:02d}: {output}")
            skipped += 1
            continue
        print(f"Requesting month {month:02d}; writing {output}")
        try:
            client.retrieve(DATASET, request, str(output))
        except Exception as exc:
            print(
                f"error: ERA5 download failed for {args.year}-{month:02d}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Downloaded {output}")
        downloaded += 1
    print(f"Finished: {downloaded} downloaded, {skipped} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
