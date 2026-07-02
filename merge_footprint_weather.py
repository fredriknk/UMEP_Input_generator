#!/usr/bin/env python3
"""Merge local Frost observations with completed ERA5 footprint NetCDF files."""

from __future__ import annotations

import argparse
import calendar
import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
from netCDF4 import Dataset, num2date


FROST_SOURCE_PRIORITY = {
    "wind_speed": ("SN76914:0",),
    "wind_from_direction": ("SN76914:0",),
    "surface_air_pressure": ("SN76914:0",),
    "air_temperature": ("SN76914:0", "SN17850:0", "SN17853:0"),
    "dew_point_temperature": ("SN17850:0", "SN17853:0"),
}
QUALITY_PRIORITY = {"0": 0, "": 1, "1": 2}
REQUIRED_ERA5_VARIABLES = {
    "valid_time",
    "blh",
    "sp",
    "t2m",
    "d2m",
    "iews",
    "inss",
    "ishf",
    "ie",
    "sdfor",
}
OUTPUT_FIELDS = [
    "valid_time",
    "wdir",
    "wind_speed",
    "t2m",
    "d2m",
    "sp",
    "blh",
    "ustar",
    "Obukhov",
    "sdfor",
    "obukhov_qc",
    "wind_direction_source",
    "wind_speed_source",
    "temperature_source",
    "dewpoint_source",
    "pressure_source",
]


class MergeError(ValueError):
    """Raised when weather inputs cannot be merged defensibly."""


@dataclass(frozen=True)
class Observation:
    value: float
    unit: str
    source: str
    quality: str


@dataclass(frozen=True)
class Era5Row:
    timestamp: datetime
    blh: float
    sp: float
    t2m: float
    d2m: float
    iews: float
    inss: float
    ishf: float
    moisture_flux: float
    sdfor: float
    u10: float | None = None
    v10: float | None = None


def parse_utc(text: str) -> datetime:
    normalized = text.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc).replace(microsecond=0)


def qswat(temperature: float, pressure: float) -> float:
    """Saturation specific humidity, following ECMWF's Obukhov example."""
    rd = 287.06
    rv = 461.53
    r2es = 611.21 * rd / rv
    retv = rv / rd - 1.0
    foeew = r2es * math.exp(
        17.502 * (temperature - 273.16) / (temperature - 32.19)
    )
    qs = foeew / pressure
    return qs / (1.0 - retv * qs)


def derive_ustar_obukhov(row: Era5Row) -> tuple[float, float]:
    """Apply ECMWF's documented ERA5 friction-velocity/Obukhov calculation."""
    rd = 287.06
    retv = 0.6078
    cp = 1004.7
    von_karman = 0.4
    gravity = 9.81

    q2 = qswat(row.d2m, row.sp)
    virtual_temperature = row.t2m * (1.0 + retv * q2)
    density = row.sp / (rd * virtual_temperature)
    stress = math.hypot(row.iews, row.inss)
    ustar = max(math.sqrt(stress / density), 0.001)

    turbulent_heat_flux = -row.ishf / (density * cp)
    turbulent_moisture_flux = -row.moisture_flux / density
    virtual_heat_flux = (
        turbulent_heat_flux + retv * row.t2m * turbulent_moisture_flux
    )
    turbulent_temperature_scale = -virtual_heat_flux / ustar
    inverse_obukhov = (
        von_karman
        * gravity
        * turbulent_temperature_scale
        / (virtual_temperature * ustar**2)
    )
    if abs(inverse_obukhov) < 1e-6:
        obukhov = 1_000_000.0 if inverse_obukhov >= 0 else -1_000_000.0
    else:
        obukhov = 1.0 / inverse_obukhov
        obukhov = max(-1_000_000.0, min(1_000_000.0, obukhov))
    return ustar, obukhov


def _scalar(variable, index: int) -> float:
    value = np.ma.asarray(variable[index]).reshape(-1)[0]
    if np.ma.is_masked(value) or not math.isfinite(float(value)):
        raise MergeError(f"Missing/non-finite ERA5 value in {variable.name}")
    return float(value)


def _optional_scalar(dataset: Dataset, name: str, index: int) -> float | None:
    if name not in dataset.variables:
        return None
    return _scalar(dataset[name], index)


def read_era5(paths: Iterable[Path]) -> dict[datetime, Era5Row]:
    rows: dict[datetime, Era5Row] = {}
    for path in sorted(paths):
        try:
            dataset = Dataset(path)
        except OSError as exc:
            raise MergeError(f"Cannot open completed ERA5 file {path}: {exc}") from exc
        with dataset:
            missing = REQUIRED_ERA5_VARIABLES - set(dataset.variables)
            if missing:
                raise MergeError(
                    f"ERA5 file {path} lacks variables: {', '.join(sorted(missing))}"
                )
            time_variable = dataset["valid_time"]
            times = num2date(
                time_variable[:],
                units=time_variable.units,
                calendar=getattr(time_variable, "calendar", "standard"),
                only_use_cftime_datetimes=False,
                only_use_python_datetimes=True,
            )
            for index, raw_time in enumerate(times):
                timestamp = raw_time.replace(tzinfo=timezone.utc, microsecond=0)
                if timestamp in rows:
                    raise MergeError(
                        f"Duplicate ERA5 timestamp {timestamp.isoformat()} from {path}"
                    )
                rows[timestamp] = Era5Row(
                    timestamp=timestamp,
                    blh=_scalar(dataset["blh"], index),
                    sp=_scalar(dataset["sp"], index),
                    t2m=_scalar(dataset["t2m"], index),
                    d2m=_scalar(dataset["d2m"], index),
                    iews=_scalar(dataset["iews"], index),
                    inss=_scalar(dataset["inss"], index),
                    ishf=_scalar(dataset["ishf"], index),
                    moisture_flux=_scalar(dataset["ie"], index),
                    sdfor=_scalar(dataset["sdfor"], index),
                    u10=_optional_scalar(dataset, "u10", index),
                    v10=_optional_scalar(dataset, "v10", index),
                )
    if not rows:
        raise MergeError("No ERA5 records were read")
    return rows


def _observation_rank(element: str, observation: Observation) -> tuple[int, int]:
    quality_rank = QUALITY_PRIORITY.get(observation.quality, 99)
    try:
        source_rank = FROST_SOURCE_PRIORITY[element].index(observation.source)
    except ValueError:
        source_rank = 99
    return quality_rank, source_rank


def read_frost(
    path: Path, wanted_times: set[datetime]
) -> dict[datetime, dict[str, Observation]]:
    result: dict[datetime, dict[str, Observation]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line_number, record in enumerate(csv.DictReader(handle), start=2):
            element = record.get("element_id", "")
            source = record.get("source_id", "")
            if (
                element not in FROST_SOURCE_PRIORITY
                or source not in FROST_SOURCE_PRIORITY[element]
            ):
                continue
            try:
                timestamp = parse_utc(record["reference_time"])
            except (KeyError, ValueError) as exc:
                raise MergeError(f"Invalid Frost timestamp at row {line_number}") from exc
            if timestamp not in wanted_times:
                continue
            quality = record.get("quality_code", "").strip()
            if quality not in QUALITY_PRIORITY:
                continue
            try:
                observation = Observation(
                    value=float(record["value"]),
                    unit=record.get("unit", ""),
                    source=source,
                    quality=quality,
                )
            except (KeyError, ValueError) as exc:
                raise MergeError(f"Invalid Frost value at row {line_number}") from exc
            current = result.setdefault(timestamp, {}).get(element)
            if current is None or _observation_rank(
                element, observation
            ) < _observation_rank(element, current):
                result[timestamp][element] = observation
    return result


def read_surface_wind_csv(
    path: Path | None, wanted_times: set[datetime]
) -> dict[datetime, tuple[float, float]]:
    if path is None:
        return {}
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line_number, record in enumerate(csv.DictReader(handle), start=2):
            try:
                timestamp = parse_utc(record["valid_time"])
            except (KeyError, ValueError) as exc:
                raise MergeError(
                    f"Invalid ERA5 surface timestamp at row {line_number}"
                ) from exc
            if timestamp not in wanted_times:
                continue
            try:
                u10 = float(record["u10"])
                v10 = float(record["v10"])
            except (KeyError, ValueError) as exc:
                raise MergeError(
                    f"Invalid ERA5 surface wind at row {line_number}"
                ) from exc
            speed = math.hypot(u10, v10)
            direction = math.degrees(math.atan2(-u10, -v10)) % 360.0
            result[timestamp] = speed, direction
    return result


def _local_value(
    observations: dict[str, Observation],
    element: str,
    fallback: float,
    conversion,
) -> tuple[float, str]:
    observation = observations.get(element)
    if observation is None:
        return fallback, "era5"
    return conversion(observation.value), (
        f"frost:{observation.source}:q{observation.quality or 'derived'}"
    )


def merge_rows(
    era5: dict[datetime, Era5Row],
    frost: dict[datetime, dict[str, Observation]],
    surface_wind: dict[datetime, tuple[float, float]],
) -> list[dict[str, str]]:
    output = []
    for timestamp, era in sorted(era5.items()):
        observations = frost.get(timestamp, {})
        fallback_wind = surface_wind.get(timestamp)
        if fallback_wind is None and era.u10 is not None and era.v10 is not None:
            fallback_wind = (
                math.hypot(era.u10, era.v10),
                math.degrees(math.atan2(-era.u10, -era.v10)) % 360.0,
            )

        speed_observation = observations.get("wind_speed")
        if speed_observation is not None:
            speed = speed_observation.value
            speed_source = (
                f"frost:{speed_observation.source}:"
                f"q{speed_observation.quality or 'derived'}"
            )
        elif fallback_wind is not None:
            speed = fallback_wind[0]
            speed_source = "era5:u10_v10"
        else:
            speed = math.nan
            speed_source = "missing"

        direction_observation = observations.get("wind_from_direction")
        if speed == 0 and fallback_wind is not None:
            direction = fallback_wind[1]
            direction_source = "era5:u10_v10:local_calm"
        elif direction_observation is not None:
            direction = direction_observation.value % 360.0
            direction_source = (
                f"frost:{direction_observation.source}:"
                f"q{direction_observation.quality or 'derived'}"
            )
        elif fallback_wind is not None:
            direction = fallback_wind[1]
            direction_source = "era5:u10_v10"
        else:
            direction = math.nan
            direction_source = "missing"

        temperature, temperature_source = _local_value(
            observations, "air_temperature", era.t2m, lambda value: value + 273.15
        )
        dewpoint, dewpoint_source = _local_value(
            observations,
            "dew_point_temperature",
            era.d2m,
            lambda value: value + 273.15,
        )
        pressure, pressure_source = _local_value(
            observations,
            "surface_air_pressure",
            era.sp,
            lambda value: value * 100.0,
        )
        ustar, obukhov = derive_ustar_obukhov(era)
        obukhov_qc = "ok" if era.sdfor < 50.0 else "sdfor_ge_50m"

        output.append(
            {
                "valid_time": timestamp.isoformat().replace("+00:00", "Z"),
                "wdir": f"{direction:.4f}" if math.isfinite(direction) else "",
                "wind_speed": f"{speed:.4f}" if math.isfinite(speed) else "",
                "t2m": f"{temperature:.3f}",
                "d2m": f"{dewpoint:.3f}",
                "sp": f"{pressure:.2f}",
                "blh": f"{era.blh:.3f}",
                "ustar": f"{ustar:.5f}",
                "Obukhov": f"{obukhov:.4f}",
                "sdfor": f"{era.sdfor:.3f}",
                "obukhov_qc": obukhov_qc,
                "wind_direction_source": direction_source,
                "wind_speed_source": speed_source,
                "temperature_source": temperature_source,
                "dewpoint_source": dewpoint_source,
                "pressure_source": pressure_source,
            }
        )
    return output


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Merge completed ERA5 footprint files with nearby Frost stations."
    )
    result.add_argument("--year", type=int, default=2025)
    result.add_argument(
        "--era5-directory", type=Path, default=Path("era_5_weatherdata")
    )
    result.add_argument(
        "--frost",
        type=Path,
        default=Path("local_weatherdata/frost_footprint_inputs_2025-2026.csv"),
    )
    result.add_argument(
        "--era5-surface-csv",
        type=Path,
        help=(
            "Optional legacy u10/v10 CSV used for missing/calm local wind. "
            "New footprint NetCDF downloads contain u10/v10 directly."
        ),
    )
    result.add_argument(
        "--output",
        type=Path,
        help="Output CSV (default: local_weatherdata/merged_footprint_weather_YEAR.csv)",
    )
    result.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow output when fewer than all hours of the requested year are present",
    )
    result.add_argument(
        "--missing-wind-policy",
        choices=("skip", "error"),
        default="skip",
        help="Skip hours lacking all wind sources, or abort (default: skip)",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output_path = args.output or Path(
        f"local_weatherdata/merged_footprint_weather_{args.year}.csv"
    )
    pattern = f"era5_footprint_parameters_{args.year}_??.nc"
    paths = sorted(args.era5_directory.glob(pattern))
    if not paths:
        print(
            f"error: no completed monthly ERA5 files match "
            f"{args.era5_directory / pattern}",
            file=sys.stderr,
        )
        return 2
    try:
        era5 = read_era5(paths)
        expected = 8784 if calendar.isleap(args.year) else 8760
        if len(era5) != expected and not args.allow_partial:
            raise MergeError(
                f"ERA5 contains {len(era5)} of {expected} expected hourly records; "
                "wait for all monthly downloads or pass --allow-partial"
            )
        wanted_times = set(era5)
        frost = read_frost(args.frost, wanted_times)
        surface_wind_path = (
            args.era5_surface_csv
            if args.era5_surface_csv and args.era5_surface_csv.is_file()
            else None
        )
        surface_wind = read_surface_wind_csv(surface_wind_path, wanted_times)
        rows = merge_rows(era5, frost, surface_wind)
        missing_wind = sum(not row["wdir"] or not row["wind_speed"] for row in rows)
        if missing_wind and args.missing_wind_policy == "error":
            raise MergeError(f"{missing_wind} output rows have no usable wind")
        if missing_wind:
            rows = [row for row in rows if row["wdir"] and row["wind_speed"]]
            if not rows:
                raise MergeError("all output rows lack usable wind")
        write_csv(output_path, rows)
    except (MergeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    flagged = sum(row["obukhov_qc"] != "ok" for row in rows)
    print(
        f"Wrote {len(rows)} merged hourly records from {len(paths)} ERA5 month(s) "
        f"to {output_path}"
    )
    if missing_wind:
        print(
            f"warning: skipped {missing_wind} hour(s) with no usable Frost or ERA5 wind",
            file=sys.stderr,
        )
    if flagged:
        print(
            f"warning: {flagged} records have sdfor >= 50 m; ECMWF advises caution "
            "for ERA5-derived Obukhov length in such grid cells",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
