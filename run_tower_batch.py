#!/usr/bin/env python
"""Run morphometry, UMEP input generation, and footprints for many towers."""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from pathlib import Path

from calculate_morphometry import calculate_for_tower, project_tower, read_towers, safe_name, write_morphology
from generate_umep_footprint_input import main as generate_input
from run_footprint_standalone import main as run_footprint


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the complete standalone footprint workflow for a tower list.")
    result.add_argument("--dom", type=Path, required=True)
    result.add_argument("--dtm", type=Path, required=True)
    result.add_argument("--towers", type=Path, required=True)
    result.add_argument("--weather", type=Path, required=True)
    result.add_argument("--measurement-height", type=float, help="Default height unless supplied per tower in CSV")
    result.add_argument("--crop-height-schedule", type=Path)
    result.add_argument("--friction-velocity", type=float)
    result.add_argument("--ustar-fraction", type=float)
    result.add_argument("--sigma-v", type=float)
    result.add_argument("--sigma-v-ustar-ratio", type=float)
    result.add_argument("--obukhov", type=float)
    result.add_argument("--boundary-layer-height", type=float)
    result.add_argument("--output-dir", type=Path, default=Path("output/towers"))
    result.add_argument(
        "--morphometry-radius", "--morphometry-distance",
        dest="morphometry_radius", type=float, default=200.0,
        help="Radius in metres used for DOM/DTM morphometry around each tower (default: 200)",
    )
    result.add_argument("--angle-step", type=float, default=5.0)
    result.add_argument("--fetch", type=float, default=2000.0)
    result.add_argument("--resolution", type=float, default=5.0)
    result.add_argument(
        "--workers", type=int, default=min(4, os.cpu_count() or 1),
        help="Parallel footprint workers per tower (default: up to 4)",
    )
    result.add_argument(
        "--placement-analysis", action="store_true",
        help="Write seasonal, stability, and wind-sector outputs for siting decisions",
    )
    result.add_argument("--growing-months", default="4-10")
    result.add_argument(
        "--display-percent", type=int, default=80,
        help="Visible cumulative footprint percentage in QGIS styles",
    )
    result.add_argument("--interpolate-resolution", type=float)
    result.add_argument("--contours", action="store_true")
    result.add_argument("--contour-levels", default="10,20,30,40,50,60,70,80")
    result.add_argument("--contour-smoothing", type=float, default=1.0)
    result.add_argument("--start")
    result.add_argument("--end")
    result.add_argument("--invalid-row-policy", choices=("skip", "error"), default="skip")
    return result


def main(argv: list[str] | None = None) -> int:
    batch_started = time.perf_counter()
    args = parser().parse_args(argv)
    if args.morphometry_radius <= 0:
        print("error: morphometry-radius must be positive", file=sys.stderr)
        return 2
    if args.angle_step <= 0 or not math.isclose(360 / args.angle_step, round(360 / args.angle_step)):
        print("error: angle-step must be positive and divide 360", file=sys.stderr)
        return 2
    if args.fetch <= 0 or args.resolution <= 0 or args.workers <= 0:
        print("error: fetch, resolution, and workers must be positive", file=sys.stderr)
        return 2
    if not 1 <= args.display_percent <= 100:
        print("error: display-percent must be between 1 and 100", file=sys.stderr)
        return 2
    if args.interpolate_resolution is not None and (
        args.interpolate_resolution <= 0
        or args.interpolate_resolution >= args.resolution
    ):
        print(
            "error: interpolate-resolution must be positive and finer than resolution",
            file=sys.stderr,
        )
        return 2
    if args.contour_smoothing < 0:
        print("error: contour-smoothing must be non-negative", file=sys.stderr)
        return 2
    if args.measurement_height is not None and args.measurement_height <= 0:
        print("error: measurement-height must be positive", file=sys.stderr)
        return 2
    try:
        towers = read_towers(args.towers)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.measurement_height is None:
        missing = [tower.name for tower in towers if tower.measurement_height is None]
        if missing:
            print("error: provide --measurement-height or measurement_height_m for every tower", file=sys.stderr)
            return 2
    safe_names = [safe_name(tower.name) for tower in towers]
    if len(set(safe_names)) != len(safe_names):
        print("error: tower IDs must be unique after filename sanitization", file=sys.stderr)
        return 2
    import rasterio
    with rasterio.open(args.dom) as source:
        raster_crs = source.crs.to_string()
    comparison_rows: list[dict[str, str]] = []
    for original_tower in towers:
        tower_started = time.perf_counter()
        tower = project_tower(original_tower, raster_crs)
        name = safe_name(tower.name)
        folder = args.output_dir / name
        morphology_path = folder / "morphology.txt"
        input_path = folder / "umep_input.txt"
        footprint_prefix = folder / "footprint"
        try:
            morphology, crs = calculate_for_tower(
                args.dom, args.dtm, tower, args.morphometry_radius, args.angle_step
            )
            write_morphology(morphology_path, morphology)
            generator_args = [
                "--morphology", str(morphology_path), "--weather", str(args.weather),
                "--output", str(input_path), "--measurement-height",
                str(
                    tower.measurement_height
                    if tower.measurement_height is not None
                    else args.measurement_height
                ),
                "--invalid-row-policy", args.invalid_row_policy,
            ]
            if args.crop_height_schedule:
                generator_args += ["--crop-height-schedule", str(args.crop_height_schedule)]
            for option in (
                "friction_velocity", "ustar_fraction", "sigma_v",
                "sigma_v_ustar_ratio", "obukhov", "boundary_layer_height",
            ):
                value = getattr(args, option)
                if value is not None:
                    generator_args += ["--" + option.replace("_", "-"), str(value)]
            if args.start:
                generator_args += ["--start", args.start]
            if args.end:
                generator_args += ["--end", args.end]
            if generate_input(generator_args):
                raise RuntimeError("UMEP input generation failed")
            footprint_args = [
                "--input", str(input_path), "--tower-x", str(tower.x), "--tower-y", str(tower.y),
                "--crs", crs, "--fetch", str(args.fetch), "--resolution", str(args.resolution),
                "--output-prefix", str(footprint_prefix), "--invalid-row-policy", args.invalid_row_policy,
                "--workers", str(args.workers),
                "--display-percent", str(args.display_percent),
                "--contour-levels", args.contour_levels,
                "--contour-smoothing", str(args.contour_smoothing),
            ]
            if args.interpolate_resolution is not None:
                footprint_args += [
                    "--interpolate-resolution", str(args.interpolate_resolution)
                ]
            if args.contours:
                footprint_args.append("--contours")
            if args.placement_analysis:
                footprint_args += [
                    "--placement-analysis",
                    "--growing-months", args.growing_months,
                ]
            if run_footprint(footprint_args):
                raise RuntimeError("footprint calculation failed")
            if args.placement_analysis:
                summary_path = footprint_prefix.with_name(
                    footprint_prefix.name + "_placement_summary.csv"
                )
                with summary_path.open(encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        comparison_rows.append(
                            {
                                "tower_id": tower.name,
                                "tower_x": f"{tower.x:.3f}",
                                "tower_y": f"{tower.y:.3f}",
                                **row,
                            }
                        )
            tower_elapsed = time.perf_counter() - tower_started
            print(
                f"{tower.name}: complete in {tower_elapsed:.2f} s "
                f"({tower_elapsed / 60.0:.2f} min) -> {folder}"
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"error: {tower.name}: {exc}", file=sys.stderr)
            return 1
    if args.placement_analysis and comparison_rows:
        comparison_path = args.output_dir / "placement_comparison.csv"
        with comparison_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=comparison_rows[0].keys())
            writer.writeheader()
            writer.writerows(comparison_rows)
        print(f"Placement comparison: {comparison_path}")
    batch_elapsed = time.perf_counter() - batch_started
    print(
        f"Total batch time: {batch_elapsed:.2f} s "
        f"({batch_elapsed / 60.0:.2f} min)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
