#!/usr/bin/env python3
"""Inlet heights that follow the crop, for two-height flux-gradient towers.

Many flux-gradient towers raise their inlets as the crop grows instead of using
one fixed height. This script turns a crop-height schedule into such an inlet
schedule and can write the matching hourly measurement height into an existing
13-column UMEP input file, so the footprint follows the inlets through the season.

Placement rule (all values configurable):

    lower inlet = max(minimum lower height, factor x crop height),
                  rounded up to the adjustment step
    upper inlet = lower inlet + separation

The default factor 2.2 equals the Kljun et al. (2015) validity limit for a crop
canopy, zm > zd + 12.5 z0, with this project's default crop roughness
(zd = 2/3 h, z0 = 0.123 h): 2/3 + 12.5 x 0.123 = 2.2. Inlets are raised as the
crop grows, held at the season's highest setting while the crop ripens and
lowered when the crop falls below the harvest threshold.

For the footprint, the effective height of an inlet pair is the geometric mean
of the two heights above the crop displacement height d:

    z_eff = d + sqrt((z_lower - d) * (z_upper - d))

Generate the UMEP input with a high fixed --measurement-height first (for
example 10 m), so that no hour is dropped before the real height is applied;
run_footprint_standalone.py then validates every hour again.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from generate_umep_footprint_input import (
    CropHeightPoint,
    InputError,
    RecurringCropHeightPoint,
    interpolate_crop_height,
    read_crop_height_schedule,
)

Schedule = list[CropHeightPoint] | list[RecurringCropHeightPoint]


@dataclass(frozen=True)
class InletRule:
    factor: float = 2.2
    minimum_lower: float = 0.5
    separation: float = 0.5
    step: float = 0.1
    harvest_below: float = 0.1
    zd_factor: float = 2.0 / 3.0


@dataclass(frozen=True)
class InletSetting:
    crop_height: float
    inlet_crop_height: float
    lower: float
    upper: float
    effective_height: float


def _crop_height_on(schedule: Schedule, day: date) -> float:
    return interpolate_crop_height(schedule, datetime(day.year, day.month, day.day))


def held_crop_height(
    schedule: Schedule,
    day: date,
    harvest_below: float = 0.1,
    cache: dict[date, float] | None = None,
) -> float:
    """Tallest crop so far this season, held until the crop drops below harvest height.

    The season restarts on 1 January or whenever the crop is below the harvest
    threshold, so the inlets come down at harvest and climb again next season.
    """
    cache = {} if cache is None else cache
    if day in cache:
        return cache[day]
    threshold = harvest_below
    first = date(day.year, 1, 1)
    current = first
    held = 0.0
    while current <= day:
        if current in cache:
            held = cache[current]
        else:
            height = _crop_height_on(schedule, current)
            held = height if height < threshold or current == first else max(height, held)
            cache[current] = held
        current += timedelta(days=1)
    return held


def inlet_setting(crop_height: float, inlet_crop_height: float, rule: InletRule) -> InletSetting:
    """Return lower/upper inlet heights and the effective footprint height (m above ground)."""
    lower = max(rule.minimum_lower, rule.factor * inlet_crop_height)
    lower = math.ceil(round(lower / rule.step, 9)) * rule.step
    upper = lower + rule.separation
    displacement = rule.zd_factor * crop_height
    effective = displacement + math.sqrt((lower - displacement) * (upper - displacement))
    return InletSetting(crop_height, inlet_crop_height, round(lower, 3), round(upper, 3), round(effective, 3))


class InletSchedule:
    """Daily inlet settings derived from a crop-height schedule."""

    def __init__(self, schedule: Schedule, rule: InletRule):
        self.schedule = schedule
        self.rule = rule
        self._cache: dict[date, float] = {}

    def on(self, day: date) -> InletSetting:
        crop = _crop_height_on(self.schedule, day)
        held = held_crop_height(self.schedule, day, self.rule.harvest_below, self._cache)
        return inlet_setting(crop, held, self.rule)


def write_schedule(inlets: InletSchedule, year: int, output: Path) -> list[tuple[date, InletSetting]]:
    rows = []
    day = date(year, 1, 1)
    while day.year == year:
        rows.append((day, inlets.on(day)))
        day += timedelta(days=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "crop_height_m", "lower_inlet_m", "upper_inlet_m", "effective_height_m"])
        for day, s in rows:
            writer.writerow([day.isoformat(), f"{s.crop_height:.3f}", f"{s.lower:.2f}", f"{s.upper:.2f}",
                             f"{s.effective_height:.3f}"])
    return rows


def apply_to_umep_input(inlets: InletSchedule, source: Path, output: Path) -> int:
    """Replace column 7 (z_m_input) of a UMEP input file with the hourly effective height."""
    lines = source.read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].split()[:1] == ["iy"]:
        raise InputError(f"{source} does not look like a 13-column UMEP input file")
    out = [lines[0]]
    for number, line in enumerate(lines[1:], start=2):
        values = line.split()
        if not values:
            continue
        if len(values) != 13:
            raise InputError(f"{source}:{number}: expected 13 columns, found {len(values)}")
        day = date(int(values[0]), 1, 1) + timedelta(days=int(values[1]) - 1)
        values[6] = f"{inlets.on(day).effective_height:.3f}"
        out.append(" ".join(values))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(out) - 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = result.add_subparsers(dest="command", required=True)
    for name, text in (("schedule", "Write a daily inlet-height table for one year"),
                       ("apply", "Write crop-following heights into a UMEP input file")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--crop-height-schedule", type=Path, required=True)
        p.add_argument("--factor", type=float, default=2.2, help="Lower inlet = factor x crop height (default 2.2)")
        p.add_argument("--minimum-lower", type=float, default=0.5, help="Lowest lower-inlet height in m (default 0.5)")
        p.add_argument("--separation", type=float, default=0.5, help="Upper minus lower inlet in m (default 0.5)")
        p.add_argument("--step", type=float, default=0.1, help="Inlet adjustment step in m (default 0.1)")
        p.add_argument("--harvest-below", type=float, default=0.1,
                       help="Crop height in m below which the inlets are lowered again (default 0.1)")
        p.add_argument("--crop-zd-factor", type=float, default=2.0 / 3.0,
                       help="Displacement height as a fraction of crop height (default 2/3, as the generator)")
        p.add_argument("--output", type=Path, required=True)
        if name == "schedule":
            p.add_argument("--year", type=int, required=True)
        else:
            p.add_argument("--input", type=Path, required=True, help="13-column UMEP input file")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.factor <= 0 or args.minimum_lower <= 0 or args.separation <= 0 or args.step <= 0:
        raise SystemExit("--factor, --minimum-lower, --separation and --step must be positive")
    rule = InletRule(args.factor, args.minimum_lower, args.separation, args.step,
                     args.harvest_below, args.crop_zd_factor)
    try:
        inlets = InletSchedule(read_crop_height_schedule(args.crop_height_schedule), rule)
        if args.command == "schedule":
            rows = write_schedule(inlets, args.year, args.output)
            previous = None
            print("Inlet changes (date: crop height -> lower / upper inlet):")
            for day, s in rows:
                if (s.lower, s.upper) != previous:
                    print(f"  {day.isoformat()}: crop {s.crop_height:.2f} m -> {s.lower:.1f} / {s.upper:.1f} m")
                    previous = (s.lower, s.upper)
        else:
            count = apply_to_umep_input(inlets, args.input, args.output)
            print(f"Wrote {count} rows with crop-following measurement heights to {args.output}")
    except InputError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
