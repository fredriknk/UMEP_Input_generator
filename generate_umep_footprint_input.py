#!/usr/bin/env python3
"""Create UMEP Source Area (Point) meteorological input files."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable


OUTPUT_HEADER = (
    "iy id it imin z_0_input z_d_input z_m_input sigv "
    "Obukhov ustar dir h por"
)
MISSING_VALUES = {"", "-999", "-999.0", "nan", "none", "null"}


class InputError(ValueError):
    """Raised when the inputs cannot produce a defensible UMEP file."""


@dataclass(frozen=True)
class Morphology:
    direction: float
    z0: float
    zd: float


@dataclass(frozen=True)
class CropHeightPoint:
    date: date
    height: float


@dataclass(frozen=True)
class OutputRow:
    timestamp: datetime
    z0: float
    zd: float
    measurement_height: float
    sigv: float
    obukhov: float
    ustar: float
    wind_direction: float
    boundary_layer_height: float
    porosity: float

    def format(self) -> str:
        doy = self.timestamp.timetuple().tm_yday
        return (
            f"{self.timestamp.year:d} {doy:d} {self.timestamp.hour:d} "
            f"{self.timestamp.minute:d} {self.z0:.4f} {self.zd:.4f} "
            f"{self.measurement_height:.3f} {self.sigv:.4f} "
            f"{self.obukhov:.4f} {self.ustar:.4f} "
            f"{self.wind_direction:.4f} {self.boundary_layer_height:.3f} "
            f"{self.porosity:.3f}"
        )


def _is_missing(value: str | None) -> bool:
    return value is None or value.strip().lower() in MISSING_VALUES


def _float(value: str | None, field: str) -> float:
    if _is_missing(value):
        raise InputError(f"Missing value for {field}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"Invalid value for {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise InputError(f"Non-finite value for {field}: {value!r}")
    return result


def read_morphology(path: Path) -> list[Morphology]:
    with path.open(encoding="utf-8-sig") as handle:
        rows = [line.split() for line in handle if line.strip()]
    if len(rows) < 2:
        raise InputError(f"No morphology data found in {path}")

    header = [name.lower() for name in rows[0]]
    try:
        wd_index = header.index("wd")
        z0_index = header.index("z0")
        zd_index = header.index("zd")
    except ValueError as exc:
        raise InputError("Morphology header must contain Wd, zd, and z0") from exc

    result = []
    for line_number, values in enumerate(rows[1:], start=2):
        try:
            result.append(
                Morphology(
                    direction=float(values[wd_index]) % 360.0,
                    z0=float(values[z0_index]),
                    zd=float(values[zd_index]),
                )
            )
        except (IndexError, ValueError) as exc:
            raise InputError(
                f"Invalid morphology row {line_number} in {path}"
            ) from exc
    if any(item.z0 < 0 or item.zd < 0 for item in result):
        raise InputError("Morphology z0 and zd must be non-negative")
    return sorted(result, key=lambda item: item.direction)


def read_crop_height_schedule(path: Path) -> list[CropHeightPoint]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise InputError(f"No crop-height schedule header found in {path}")
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        try:
            date_field = fields["date"]
            height_field = fields["height_m"]
        except KeyError as exc:
            raise InputError(
                "Crop-height schedule header must contain date,height_m"
            ) from exc

        result = []
        for line_number, row in enumerate(reader, start=2):
            try:
                point_date = date.fromisoformat(row[date_field].strip())
                height = _float(row[height_field], "crop height")
            except (AttributeError, ValueError) as exc:
                raise InputError(
                    f"Invalid crop-height schedule row {line_number} in {path}"
                ) from exc
            if height < 0:
                raise InputError(
                    f"Crop height cannot be negative at row {line_number} in {path}"
                )
            result.append(CropHeightPoint(point_date, height))

    if not result:
        raise InputError(f"No crop-height records found in {path}")
    result.sort(key=lambda item: item.date)
    if len({item.date for item in result}) != len(result):
        raise InputError("Crop-height schedule dates must be unique")
    return result


def interpolate_crop_height(
    schedule: list[CropHeightPoint], timestamp: datetime
) -> float:
    """Linearly interpolate crop height; hold endpoint values outside the schedule."""
    if not schedule:
        raise InputError("No crop-height schedule values are available")
    target = timestamp.date()
    if target <= schedule[0].date:
        return schedule[0].height
    if target >= schedule[-1].date:
        return schedule[-1].height
    for lower, upper in zip(schedule, schedule[1:]):
        if lower.date <= target <= upper.date:
            span = (upper.date - lower.date).days
            weight = (target - lower.date).days / span
            return lower.height + weight * (upper.height - lower.height)
    raise AssertionError("Crop-height interpolation failed")


def interpolate_morphology(
    morphology: list[Morphology], direction: float
) -> Morphology:
    """Linearly interpolate z0 and zd, including across geographic north."""
    if not morphology:
        raise InputError("No directional morphology values are available")
    if len(morphology) == 1:
        item = morphology[0]
        return Morphology(direction % 360.0, item.z0, item.zd)

    target = direction % 360.0
    extended = [
        Morphology(morphology[-1].direction - 360.0, morphology[-1].z0, morphology[-1].zd),
        *morphology,
        Morphology(morphology[0].direction + 360.0, morphology[0].z0, morphology[0].zd),
    ]
    for lower, upper in zip(extended, extended[1:]):
        if lower.direction <= target <= upper.direction:
            span = upper.direction - lower.direction
            weight = 0.0 if span == 0 else (target - lower.direction) / span
            return Morphology(
                target,
                lower.z0 + weight * (upper.z0 - lower.z0),
                lower.zd + weight * (upper.zd - lower.zd),
            )
    raise AssertionError("Circular interpolation failed")


def interpolate_morphology_with_crop(
    morphology: list[Morphology],
    direction: float,
    crop_height: float,
    z0_factor: float = 0.123,
    zd_factor: float = 2.0 / 3.0,
    minimum_z0: float = 0.01,
) -> Morphology:
    """Fill zero-object sectors with a crop aerodynamic baseline, then interpolate."""
    crop_z0 = max(minimum_z0, z0_factor * crop_height)
    crop_zd = zd_factor * crop_height
    filled = [
        item
        if item.z0 > 0
        else Morphology(item.direction, crop_z0, crop_zd)
        for item in morphology
    ]
    return interpolate_morphology(filled, direction)


def read_weather(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        handle.seek(0)
        delimiter = "," if "," in first_line else None
        if delimiter:
            rows = list(csv.DictReader(handle))
        else:
            header = first_line.split()
            rows = [
                dict(zip(header, line.split()))
                for line in handle
                if line.strip() and line != first_line
            ]
    if not rows:
        raise InputError(f"No weather records found in {path}")
    return rows


def _first(row: dict[str, str], aliases: Iterable[str]) -> str | None:
    lower = {key.lower(): value for key, value in row.items()}
    for alias in aliases:
        value = lower.get(alias.lower())
        if not _is_missing(value):
            return value
    return None


def parse_timestamp(row: dict[str, str]) -> datetime:
    text = _first(row, ("valid_time", "time", "datetime", "timestamp"))
    if text is not None:
        normalized = text.strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(normalized)
            if result.tzinfo is not None:
                result = result.astimezone(timezone.utc).replace(tzinfo=None)
            return result
        except ValueError as exc:
            raise InputError(f"Unsupported timestamp: {text!r}") from exc

    year = int(_float(_first(row, ("iy", "year")), "year"))
    doy = int(_float(_first(row, ("id", "doy", "day_of_year")), "day of year"))
    hour = int(_float(_first(row, ("it", "hour")), "hour"))
    minute = int(_float(_first(row, ("imin", "minute")), "minute"))
    try:
        return datetime.strptime(
            f"{year} {doy:03d} {hour:02d} {minute:02d}", "%Y %j %H %M"
        )
    except ValueError as exc:
        raise InputError("Invalid year/day/hour/minute weather timestamp") from exc


def wind_direction(row: dict[str, str]) -> float:
    direct = _first(row, ("wdir", "dir", "wind_direction"))
    if direct is not None:
        return _float(direct, "wind direction") % 360.0
    u = _float(_first(row, ("u10", "10u")), "10 m eastward wind")
    v = _float(_first(row, ("v10", "10v")), "10 m northward wind")
    # Meteorological direction: where the wind comes from, clockwise from north.
    return math.degrees(math.atan2(-u, -v)) % 360.0


def wind_speed(row: dict[str, str]) -> float:
    direct = _first(row, ("u", "wind_speed", "windspeed"))
    if direct is not None:
        return _float(direct, "wind speed")
    u = _float(_first(row, ("u10", "10u")), "10 m eastward wind")
    v = _float(_first(row, ("v10", "10v")), "10 m northward wind")
    return math.hypot(u, v)


def value_or_fallback(
    row: dict[str, str],
    aliases: Iterable[str],
    label: str,
    fallback: float | None,
) -> float:
    value = _first(row, aliases)
    if value is not None:
        return _float(value, label)
    if fallback is None:
        raise InputError(
            f"Weather data have no usable {label}; provide the corresponding "
            "fallback option explicitly"
        )
    return fallback


def build_rows(
    weather: list[dict[str, str]],
    morphology: list[Morphology],
    args: argparse.Namespace,
    crop_schedule: list[CropHeightPoint] | None = None,
) -> list[OutputRow]:
    output = []
    for index, record in enumerate(weather, start=2):
        try:
            timestamp = parse_timestamp(record)
            direction = wind_direction(record)
            if crop_schedule is None:
                morph = interpolate_morphology(morphology, direction)
            else:
                crop_height = interpolate_crop_height(crop_schedule, timestamp)
                morph = interpolate_morphology_with_crop(
                    morphology,
                    direction,
                    crop_height,
                    z0_factor=args.crop_z0_factor,
                    zd_factor=args.crop_zd_factor,
                    minimum_z0=args.minimum_z0,
                )
            ustar = value_or_fallback(
                record,
                ("ustar", "friction_velocity"),
                "friction velocity",
                args.friction_velocity,
            )
            if _first(record, ("ustar", "friction_velocity")) is None and args.ustar_fraction:
                ustar = args.ustar_fraction * wind_speed(record)
            sigv = value_or_fallback(
                record,
                ("sigv", "sigma_v", "standard_deviation_lateral_wind"),
                "lateral wind standard deviation",
                args.sigma_v,
            )
            if (
                _first(record, ("sigv", "sigma_v", "standard_deviation_lateral_wind"))
                is None
                and args.sigma_v_ustar_ratio
            ):
                sigv = args.sigma_v_ustar_ratio * ustar
            output.append(
                OutputRow(
                    timestamp=timestamp,
                    z0=morph.z0,
                    zd=morph.zd,
                    measurement_height=args.measurement_height,
                    sigv=sigv,
                    obukhov=value_or_fallback(
                        record,
                        ("obukhov", "ol", "monin_obukhov_length"),
                        "Obukhov length",
                        args.obukhov,
                    ),
                    ustar=ustar,
                    wind_direction=direction,
                    boundary_layer_height=value_or_fallback(
                        record,
                        ("h", "blh", "boundary_layer_height"),
                        "boundary-layer height",
                        args.boundary_layer_height,
                    ),
                    porosity=args.porosity,
                )
            )
        except InputError as exc:
            raise InputError(f"Weather row {index}: {exc}") from exc
    return output


def validate_rows(rows: list[OutputRow], model: str) -> list[str]:
    errors = []
    for row_number, row in enumerate(rows, start=2):
        effective_height = row.measurement_height - row.zd
        prefix = f"row {row_number} ({row.timestamp.isoformat()}): "
        if effective_height <= 0:
            errors.append(
                prefix
                + f"measurement height {row.measurement_height:.2f} m is not above "
                + f"displacement height {row.zd:.2f} m"
            )
            continue
        if row.sigv <= 0 or row.ustar <= 0:
            errors.append(prefix + "sigv and ustar must be positive")
        if model == "kljun":
            if abs(row.obukhov) < 1e-9:
                errors.append(prefix + "Kljun requires non-zero Obukhov length")
                continue
            required = row.zd + 12.5 * row.z0
            if row.measurement_height <= required:
                errors.append(
                    prefix
                    + f"Kljun requires sensor height > zd + 12.5*z0 "
                    + f"({required:.2f} m for this wind direction)"
                )
            if row.ustar <= 0.1:
                errors.append(prefix + "Kljun requires ustar > 0.1 m/s")
            if row.boundary_layer_height <= 10:
                errors.append(prefix + "Kljun requires boundary-layer height > 10 m")
            if effective_height >= row.boundary_layer_height:
                errors.append(prefix + "effective sensor height must be below PBL height")
            if effective_height / row.obukhov < -15.5:
                errors.append(prefix + "Kljun requires (height - zd)/Obukhov >= -15.5")
    return errors


def write_output(path: Path, rows: list[OutputRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(OUTPUT_HEADER + "\n")
        for row in rows:
            handle.write(row.format() + "\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Generate a 13-column UMEP Source Area (Point) input file."
    )
    result.add_argument("--morphology", type=Path, required=True)
    result.add_argument("--weather", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--measurement-height", type=float, required=True)
    result.add_argument("--model", choices=("kljun", "kormann"), default="kljun")
    result.add_argument("--porosity", type=float, default=60.0)
    result.add_argument(
        "--start",
        help="Include timestamps at or after this ISO date/time (for example 2025-01-01)",
    )
    result.add_argument(
        "--end",
        help="Exclude timestamps at or after this ISO date/time (for example 2026-01-01)",
    )
    result.add_argument("--friction-velocity", type=float)
    result.add_argument("--ustar-fraction", type=float)
    result.add_argument("--sigma-v", type=float)
    result.add_argument("--sigma-v-ustar-ratio", type=float)
    result.add_argument("--obukhov", type=float)
    result.add_argument("--boundary-layer-height", type=float)
    result.add_argument(
        "--crop-height-schedule",
        type=Path,
        help=(
            "CSV with date,height_m rows. Zero-object morphology sectors receive "
            "date-interpolated crop roughness."
        ),
    )
    result.add_argument(
        "--crop-z0-factor",
        type=float,
        default=0.123,
        help="Crop momentum roughness as a fraction of crop height (default: 0.123)",
    )
    result.add_argument(
        "--crop-zd-factor",
        type=float,
        default=2.0 / 3.0,
        help="Crop displacement height as a fraction of crop height (default: 2/3)",
    )
    result.add_argument(
        "--minimum-z0",
        type=float,
        default=0.01,
        help="Minimum roughness for bare/emerging zero-object sectors in metres",
    )
    result.add_argument(
        "--invalid-row-policy",
        choices=("error", "skip"),
        default="error",
        help=(
            "Fail on physically invalid records, or omit them and report the count "
            "(default: error)"
        ),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 0 <= args.porosity <= 100:
        print("error: porosity must be between 0 and 100 percent", file=sys.stderr)
        return 2
    if args.measurement_height <= 0:
        print("error: measurement height must be positive", file=sys.stderr)
        return 2
    if args.crop_z0_factor < 0 or args.crop_zd_factor < 0 or args.minimum_z0 <= 0:
        print(
            "error: crop roughness factors must be non-negative and minimum z0 positive",
            file=sys.stderr,
        )
        return 2
    if args.friction_velocity is None and args.ustar_fraction is not None:
        args.friction_velocity = 0.0  # Enables the explicit derived fallback.
    if args.sigma_v is None and args.sigma_v_ustar_ratio is not None:
        args.sigma_v = 0.0  # Enables the explicit derived fallback.

    try:
        morphology = read_morphology(args.morphology)
        crop_schedule = (
            read_crop_height_schedule(args.crop_height_schedule)
            if args.crop_height_schedule
            else None
        )
        if crop_schedule is None and any(item.z0 == 0 for item in morphology):
            raise InputError(
                "Morphology contains zero-roughness sectors; provide "
                "--crop-height-schedule (or use a morphology file with positive z0)"
            )
        weather = read_weather(args.weather)
        if args.start:
            start = datetime.fromisoformat(args.start)
            weather = [record for record in weather if parse_timestamp(record) >= start]
        if args.end:
            end = datetime.fromisoformat(args.end)
            weather = [record for record in weather if parse_timestamp(record) < end]
        if not weather:
            raise InputError("No weather records remain after date filtering")
        rows = build_rows(weather, morphology, args, crop_schedule)
        errors = validate_rows(rows, args.model)
        skipped = 0
        if errors and args.invalid_row_policy == "skip":
            valid_rows = [
                row for row in rows if not validate_rows([row], args.model)
            ]
            skipped = len(rows) - len(valid_rows)
            rows = valid_rows
            if not rows:
                raise InputError("All weather records failed UMEP physical validation")
        elif errors:
            preview = "\n".join(f"  - {error}" for error in errors[:10])
            remainder = (
                f"\n  - ... and {len(errors) - 10} more error(s)"
                if len(errors) > 10
                else ""
            )
            raise InputError(
                "UMEP physical validation failed:\n" + preview + remainder
            )
        write_output(args.output, rows)
    except (InputError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Wrote {len(rows)} UMEP records to {args.output}")
    if skipped:
        print(
            f"Skipped {skipped} physically invalid record(s); "
            "typically obstructed wind sectors or failed Kljun limits.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
