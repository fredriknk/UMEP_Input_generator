#!/usr/bin/env python
"""Standalone Kljun et al. (2015) footprint climatology runner.

The numerical parameterisation follows the FFP implementation distributed with
UMEP, separated from QGIS and its GUI. Input is the 13-column UMEP Source Area
(Point) meteorological file produced by generate_umep_footprint_input.py.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np


HEADER = (
    "iy", "id", "it", "imin", "z_0_input", "z_d_input", "z_m_input",
    "sigv", "Obukhov", "ustar", "dir", "h", "por",
)


@dataclass(frozen=True)
class Grid:
    fetch: float
    resolution: float

    @property
    def size(self) -> int:
        return int(math.ceil(2 * self.fetch / self.resolution))

    def coordinates(self) -> tuple[np.ndarray, np.ndarray]:
        n = self.size
        axis = -self.fetch + (np.arange(n, dtype=np.float64) + 0.5) * self.resolution
        return np.meshgrid(axis, axis)


def read_umep_input(path: Path) -> np.ndarray:
    with path.open("r", encoding="utf-8-sig") as handle:
        first = handle.readline().strip().split()
    if tuple(first) != HEADER:
        raise ValueError(
            f"unexpected header in {path}; expected exactly: {' '.join(HEADER)}"
        )
    data = np.loadtxt(path, skiprows=1, ndmin=2)
    if data.shape[1] != len(HEADER):
        raise ValueError(f"{path} has {data.shape[1]} columns; expected 13")
    if not np.isfinite(data).all():
        raise ValueError(f"{path} contains NaN or infinite values")
    return data


def validate_row(row: np.ndarray) -> str | None:
    z0, zd, zag, sigv, ol, ustar, wind_dir, pblh = row[4:12]
    zm = zag - zd
    if z0 <= 0:
        return "z0_not_positive"
    if zm <= 0:
        return "effective_height_not_positive"
    if ustar <= 0.1:
        return "ustar_not_above_0.1"
    if sigv <= 0:
        return "sigv_not_positive"
    if pblh <= 10:
        return "boundary_layer_not_above_10m"
    if zm >= pblh:
        return "measurement_above_boundary_layer"
    if not 0 <= wind_dir <= 360:
        return "wind_direction_out_of_range"
    if abs(ol) < 1e-9:
        return "obukhov_zero"
    return None


def footprint_for_row(
    row: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
) -> np.ndarray:
    """Return footprint density [m-2] on a tower-centred grid."""
    z0, zd, zag, sigv, ol, ustar, wind_dir, pblh = row[4:12]
    zm = zag - zd

    a, b, c, d = 1.4524, -1.9914, 1.4622, 0.1359
    ac, bc, cc = 2.17, 1.66, 20.0
    neutral_limit = 5000.0

    # Direct rotation is algebraically identical to the old polar-coordinate
    # route, but avoids two expensive full-grid transcendental operations.
    wind_radians = wind_dir * np.pi / 180.0
    sin_wind, cos_wind = math.sin(wind_radians), math.cos(wind_radians)
    alongwind = y * cos_wind + x * sin_wind
    crosswind = x * cos_wind - y * sin_wind

    if ol <= 0 or ol >= neutral_limit:
        xx = (1.0 - 19.0 * zm / ol) ** 0.25
        psi_f = (
            np.log((1.0 + xx**2) / 2.0)
            + 2.0 * np.log((1.0 + xx) / 2.0)
            - 2.0 * np.arctan(xx)
            + np.pi / 2.0
        )
    else:
        psi_f = -5.3 * zm / ol

    denominator = np.log(zm / z0) - psi_f
    if denominator <= 0:
        raise ValueError("non-positive logarithmic wind-profile denominator")

    height_factor = 1.0 - zm / pblh
    xstar = alongwind / zm * height_factor / denominator
    valid = xstar > d

    result = np.zeros_like(x, dtype=np.float64)
    if not np.any(valid):
        return result

    shifted = xstar[valid] - d
    fstar_ci = a * shifted**b * np.exp(-c / shifted)
    f_ci = fstar_ci / zm * height_factor / denominator
    sigystar = ac * np.sqrt(bc * np.abs(xstar[valid]) ** 2 / (1 + cc * np.abs(xstar[valid])))

    ol_for_scale = -1e6 if abs(ol) > neutral_limit else ol
    base = 0.80 if ol_for_scale <= 0 else 0.55
    scale_const = min(1.0, 1e-5 / abs(zm / ol_for_scale) + base)
    sigy = sigystar / scale_const * zm * sigv / ustar

    result[valid] = (
        f_ci
        / (np.sqrt(2 * np.pi) * sigy)
        * np.exp(-(crosswind[valid] ** 2) / (2 * sigy**2))
    )
    result[~np.isfinite(result)] = 0
    return result


def contribution_percent_raster(
    footprint: np.ndarray, resolution: float
) -> tuple[np.ndarray, float]:
    """Return cumulative percent rank (1..100) and mass captured by the domain."""
    cell_mass = np.maximum(footprint, 0) * resolution**2
    total = float(cell_mass.sum())
    ranks = np.zeros(footprint.shape, dtype=np.uint8)
    if total <= 0:
        return ranks, 0.0
    flat = cell_mass.ravel()
    order = np.argsort(flat)[::-1]
    cumulative = np.cumsum(flat[order]) / total
    values = np.minimum(100, np.maximum(1, np.ceil(cumulative * 100))).astype(np.uint8)
    ranks.ravel()[order] = values
    ranks.ravel()[flat == 0] = 0
    return ranks, total


def write_geotiffs(
    output_prefix: Path,
    footprint: np.ndarray,
    percent: np.ndarray,
    grid: Grid,
    tower_x: float,
    tower_y: float,
    crs: str,
) -> tuple[Path, Path]:
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ImportError as exc:
        raise RuntimeError(
            "rasterio is required; run: python -m pip install -r requirements.txt"
        ) from exc

    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    transform = from_origin(
        tower_x - grid.fetch,
        tower_y + grid.fetch,
        grid.resolution,
        grid.resolution,
    )
    density_path, percent_path = _select_output_paths(output_prefix)

    common = {
        "driver": "GTiff",
        "height": grid.size,
        "width": grid.size,
        "count": 1,
        "crs": crs,
        "transform": transform,
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(density_path, "w", dtype="float32", nodata=0.0, **common) as dst:
        dst.write(np.flipud(footprint).astype(np.float32), 1)
        dst.set_band_description(1, "Kljun 2015 footprint density (m-2)")
    with rasterio.open(percent_path, "w", dtype="uint8", nodata=0, **common) as dst:
        dst.write(np.flipud(percent), 1)
        dst.set_band_description(1, "Cumulative contribution percentage")
    return density_path, percent_path


def _can_replace(path: Path) -> bool:
    """Return whether an existing file can be renamed/replaced on this system."""
    if not path.exists():
        return True
    probe = path.with_name(path.name + ".lockcheck")
    try:
        path.replace(probe)
        probe.replace(path)
        return True
    except OSError:
        # If the first rename succeeded but the second did not, make a best
        # effort to restore the original name before reporting the lock.
        if probe.exists() and not path.exists():
            try:
                probe.replace(path)
            except OSError:
                pass
        return False


def _select_output_paths(output_prefix: Path) -> tuple[Path, Path]:
    """Keep density/percent pairs together when QGIS locks an earlier result."""
    def paths(prefix: Path) -> tuple[Path, Path]:
        return (
            prefix.with_name(prefix.name + "_density.tif"),
            prefix.with_name(prefix.name + "_percent.tif"),
        )

    density, percent = paths(output_prefix)
    if _can_replace(density) and _can_replace(percent):
        return density, percent

    run_number = 2
    while True:
        candidate = output_prefix.with_name(f"{output_prefix.name}_run{run_number}")
        density, percent = paths(candidate)
        if not density.exists() and not percent.exists():
            logging.getLogger("footprint").warning(
                "Existing output is open or locked; writing this run as %s_*",
                candidate,
            )
            return density, percent
        run_number += 1


def write_qgis_styles(
    density_path: Path,
    percent_path: Path,
    density_max: float,
    display_percent: int = 80,
) -> tuple[Path, Path]:
    """Write same-basename QGIS styles so newly added rasters render usefully."""
    density_qml = density_path.with_suffix(".qml")
    percent_qml = percent_path.with_suffix(".qml")
    ramp_values = (
        max(1, round(display_percent * 0.25)),
        max(1, round(display_percent * 0.50)),
        max(1, round(display_percent * 0.75)),
    )
    hidden_minimum = display_percent + 1
    # Closely follows UMEP/FootprintModel/footprint_style.qml. QGIS interprets
    # alpha in the ramp as per-cell transparency and renderer opacity globally.
    percent_qml.write_text(
        """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="0.50"
                    alphaBand="-1" classificationMin="1" classificationMax="100">
      <rasterTransparency>
        <singleValuePixelList>
          <pixelListEntry min="0" max="0" percentTransparent="100"/>
          <pixelListEntry min="{hidden_minimum}" max="100" percentTransparent="100"/>
        </singleValuePixelList>
      </rasterTransparency>
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" clip="0"
                         classificationMode="1" minimumValue="1" maximumValue="100">
          <item alpha="255" value="1" label="Highest contribution" color="#d7191c"/>
          <item alpha="255" value="{ramp_1}" label="{ramp_1}%" color="#fdae61"/>
          <item alpha="255" value="{ramp_2}" label="{ramp_2}%" color="#ffffbf"/>
          <item alpha="255" value="{ramp_3}" label="{ramp_3}%" color="#abdda4"/>
          <item alpha="255" value="{display_percent}" label="{display_percent}% boundary" color="#2b83ba"/>
          <rampLegendSettings direction="0" minimumLabel="High" maximumLabel="Low"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" grayscaleMode="0" saturation="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
""".format(
            display_percent=display_percent,
            hidden_minimum=hidden_minimum,
            ramp_1=ramp_values[0],
            ramp_2=ramp_values[1],
            ramp_3=ramp_values[2],
        ),
        encoding="utf-8",
    )
    maximum = max(float(density_max), np.finfo(np.float32).tiny)
    stops = (maximum * 0.002, maximum * 0.02, maximum * 0.12, maximum * 0.45, maximum)
    density_qml.write_text(
        f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="0.68"
                    alphaBand="-1" classificationMin="0" classificationMax="{maximum:.12g}">
      <rasterTransparency>
        <singleValuePixelList>
          <pixelListEntry min="0" max="0" percentTransparent="100"/>
        </singleValuePixelList>
      </rasterTransparency>
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" clip="1"
                         classificationMode="1" minimumValue="0" maximumValue="{maximum:.12g}">
          <item alpha="0" value="0" label="No contribution" color="#2b83ba"/>
          <item alpha="90" value="{stops[0]:.12g}" label="Very low" color="#2b83ba"/>
          <item alpha="145" value="{stops[1]:.12g}" label="Low" color="#abdda4"/>
          <item alpha="185" value="{stops[2]:.12g}" label="Moderate" color="#ffffbf"/>
          <item alpha="225" value="{stops[3]:.12g}" label="High" color="#fdae61"/>
          <item alpha="255" value="{stops[4]:.12g}" label="Peak contribution" color="#d7191c"/>
          <rampLegendSettings direction="0" minimumLabel="Low" maximumLabel="High"/>
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation colorizeOn="0" grayscaleMode="0" saturation="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
""",
        encoding="utf-8",
    )
    return density_qml, percent_qml


def _process_rows(
    rows: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    invalid_row_policy: str,
    placement_analysis: bool = False,
    growing_months: frozenset[int] = frozenset(range(4, 11)),
) -> tuple[
    dict[str, np.ndarray],
    dict[str, int],
    list[tuple[int, int, int, int, str]],
]:
    """Calculate one independent row chunk for sequential or threaded use."""
    totals: dict[str, np.ndarray] = {"annual": np.zeros(x.shape, dtype=np.float64)}
    counts: dict[str, int] = {"annual": 0}
    qc_rows: list[tuple[int, int, int, int, str]] = []
    for row in rows:
        reason = validate_row(row)
        if reason:
            qc_rows.append((int(row[0]), int(row[1]), int(row[2]), int(row[3]), reason))
            if invalid_row_policy == "error":
                raise ValueError(
                    f"{int(row[0])}-{int(row[1]):03d} {int(row[2]):02d}:{int(row[3]):02d}: {reason}"
                )
            continue
        try:
            footprint = footprint_for_row(row, x, y)
            labels = ["annual"]
            if placement_analysis:
                labels.extend(_placement_labels(row, growing_months))
            for label in labels:
                if label not in totals:
                    totals[label] = np.zeros(x.shape, dtype=np.float64)
                    counts[label] = 0
                totals[label] += footprint
                counts[label] += 1
        except ValueError as exc:
            qc_rows.append((int(row[0]), int(row[1]), int(row[2]), int(row[3]), str(exc)))
            if invalid_row_policy == "error":
                raise
    return totals, counts, qc_rows


def _placement_labels(row: np.ndarray, growing_months: frozenset[int]) -> tuple[str, str, str]:
    """Return season, stability, and meteorological wind-sector labels."""
    year, day_of_year = int(row[0]), int(row[1])
    month = (datetime(year, 1, 1) + timedelta(days=day_of_year - 1)).month
    season = "growing" if month in growing_months else "dormant"

    zm = row[6] - row[5]
    stability_value = zm / row[8]
    if stability_value < -0.05:
        stability = "unstable"
    elif stability_value > 0.05:
        stability = "stable"
    else:
        stability = "neutral"

    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    sector = directions[int(((row[10] % 360.0) + 22.5) // 45.0) % 8]
    return season, f"stability_{stability}", f"wind_{sector}"


def run(args: argparse.Namespace) -> int:
    log = logging.getLogger("footprint")
    rows = read_umep_input(args.input)
    if args.limit is not None:
        rows = rows[: args.limit]
    grid = Grid(args.fetch, args.resolution)
    x, y = grid.coordinates()
    growing_months = frozenset(args.growing_months)
    log.info(
        "Loaded %d rows; grid %dx%d at %.2f m (%.1f m fetch); workers=%d",
        len(rows), grid.size, grid.size, grid.resolution, grid.fetch, args.workers,
    )
    if args.workers == 1 or len(rows) < 2:
        totals, counts, qc_rows = _process_rows(
            rows, x, y, args.invalid_row_policy,
            args.placement_analysis, growing_months,
        )
    else:
        # Several fairly large chunks reduce scheduling overhead and bound the
        # number of per-worker accumulation grids held in memory.
        chunk_count = min(len(rows), args.workers * 4)
        chunks = [chunk for chunk in np.array_split(rows, chunk_count) if len(chunk)]
        totals: dict[str, np.ndarray] = {}
        counts: dict[str, int] = {}
        qc_rows = []
        processed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    _process_rows, chunk, x, y, args.invalid_row_policy,
                    args.placement_analysis, growing_months,
                ): len(chunk)
                for chunk in chunks
            }
            for future in as_completed(futures):
                partial_totals, partial_counts, qc = future.result()
                for label, partial in partial_totals.items():
                    if label not in totals:
                        totals[label] = np.zeros(x.shape, dtype=np.float64)
                        counts[label] = 0
                    totals[label] += partial
                    counts[label] += partial_counts[label]
                qc_rows.extend(qc)
                processed += futures[future]
                log.info(
                    "Processed %d/%d rows (%d valid, %d skipped)",
                    processed, len(rows), counts.get("annual", 0), len(qc_rows),
                )
        qc_rows.sort(key=lambda item: item[:4])

    valid_count = counts.get("annual", 0)
    if not valid_count:
        raise ValueError("no valid footprints were calculated")
    climatology = totals["annual"] / valid_count
    percent, captured_mass = contribution_percent_raster(climatology, grid.resolution)
    density_path, percent_path = write_geotiffs(
        args.output_prefix, climatology, percent, grid,
        args.tower_x, args.tower_y, args.crs,
    )
    density_qml, percent_qml = write_qgis_styles(
        density_path, percent_path, float(climatology.max()), args.display_percent
    )

    qc_path = args.output_prefix.with_name(args.output_prefix.name + "_qc.csv")
    with qc_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("iy", "id", "it", "imin", "status"))
        writer.writerows(qc_rows)

    log.info("Wrote %s", density_path)
    log.info("Wrote %s", percent_path)
    log.info("Wrote QGIS styles %s and %s", density_qml, percent_qml)
    log.info(
        "Valid rows: %d; skipped: %d; footprint mass inside raster: %.4f",
        valid_count, len(qc_rows), captured_mass,
    )
    if captured_mass < 0.8:
        log.warning("Less than 80%% of the modelled mass is inside the selected fetch")

    if args.placement_analysis:
        summary_path = args.output_prefix.with_name(
            args.output_prefix.name + "_placement_summary.csv"
        )
        summary_rows = [
            ("annual", valid_count, captured_mass,
             int(np.count_nonzero((percent > 0) & (percent <= 80))) * grid.resolution**2)
        ]
        preferred_order = (
            "growing", "dormant",
            "stability_unstable", "stability_neutral", "stability_stable",
            "wind_N", "wind_NE", "wind_E", "wind_SE",
            "wind_S", "wind_SW", "wind_W", "wind_NW",
        )
        for label in preferred_order:
            count = counts.get(label, 0)
            if not count:
                continue
            category = totals[label] / count
            category_percent, mass = contribution_percent_raster(
                category, grid.resolution
            )
            category_prefix = args.output_prefix.with_name(
                args.output_prefix.name + "_" + label
            )
            category_density, category_percent_path = write_geotiffs(
                category_prefix, category, category_percent, grid,
                args.tower_x, args.tower_y, args.crs,
            )
            write_qgis_styles(
                category_density, category_percent_path, float(category.max()),
                args.display_percent,
            )
            area80 = (
                int(np.count_nonzero(
                    (category_percent > 0) & (category_percent <= 80)
                ))
                * grid.resolution**2
            )
            summary_rows.append((label, count, mass, area80))
        with summary_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("category", "valid_hours", "captured_mass", "area80_m2"))
            writer.writerows(summary_rows)
        log.info("Wrote placement analysis and %s", summary_path)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Kljun et al. (2015) footprint model without QGIS."
    )
    parser.add_argument("--input", type=Path, required=True, help="13-column UMEP input text file")
    parser.add_argument("--tower-x", type=float, required=True, help="Tower easting in the output CRS")
    parser.add_argument("--tower-y", type=float, required=True, help="Tower northing in the output CRS")
    parser.add_argument("--crs", default="EPSG:25832", help="Projected CRS, default EPSG:25832")
    parser.add_argument("--fetch", type=float, default=2000.0, help="Maximum fetch in metres")
    parser.add_argument("--resolution", type=float, default=5.0, help="Output cell size in metres")
    parser.add_argument("--output-prefix", type=Path, default=Path("output/standalone_footprint"))
    parser.add_argument("--invalid-row-policy", choices=("skip", "error"), default="skip")
    parser.add_argument("--log-every", type=int, default=50)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Parallel footprint workers (default: up to 4; use 1 for sequential)",
    )
    parser.add_argument(
        "--placement-analysis",
        action="store_true",
        help="Also write growing/dormant, stability, and eight wind-sector climatologies",
    )
    parser.add_argument(
        "--display-percent",
        type=int,
        default=80,
        help="QGIS style displays this cumulative footprint percentage (default: 80)",
    )
    parser.add_argument(
        "--growing-months",
        default="4-10",
        help="Growing-season months, e.g. 4-10 or 4,5,6,7,8,9,10 (default: 4-10)",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N rows (for testing)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.fetch <= 0 or args.resolution <= 0:
        parser.error("--fetch and --resolution must be positive")
    if args.log_every <= 0:
        parser.error("--log-every must be positive")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if not 1 <= args.display_percent <= 100:
        parser.error("--display-percent must be between 1 and 100")
    try:
        args.growing_months = parse_months(args.growing_months)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def parse_months(value: str) -> tuple[int, ...]:
    """Parse comma-separated months and inclusive ranges."""
    months: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError("growing-month ranges must be ascending")
            months.update(range(start, end + 1))
        else:
            months.add(int(part))
    if not months or any(month < 1 or month > 12 for month in months):
        raise ValueError("growing months must be between 1 and 12")
    return tuple(sorted(months))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        logging.getLogger("footprint").error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
