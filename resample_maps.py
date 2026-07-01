#!/usr/bin/env python
"""Resample an aligned DOM/DTM pair to a smaller, compressed common grid."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Resample matching DOM/DTM rasters to an aligned compressed grid."
    )
    result.add_argument("--dom", type=Path, required=True)
    result.add_argument("--dtm", type=Path, required=True)
    result.add_argument("--output-dom", type=Path, required=True)
    result.add_argument("--output-dtm", type=Path, required=True)
    result.add_argument("--resolution", type=float, default=2.0, help="Output pixel size in CRS units")
    result.add_argument(
        "--method",
        choices=("bilinear", "average", "nearest"),
        default="bilinear",
        help="Elevation resampling method (default: bilinear)",
    )
    return result


def resample_pair(args: argparse.Namespace) -> None:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import reproject

    if args.resolution <= 0:
        raise ValueError("resolution must be positive")
    if args.output_dom.resolve() == args.dom.resolve() or args.output_dtm.resolve() == args.dtm.resolve():
        raise ValueError("output paths must differ from input paths")

    method = {
        "bilinear": Resampling.bilinear,
        "average": Resampling.average,
        "nearest": Resampling.nearest,
    }[args.method]

    with rasterio.open(args.dom) as dom, rasterio.open(args.dtm) as dtm:
        if dom.crs != dtm.crs or dom.transform != dtm.transform or dom.shape != dtm.shape:
            raise ValueError("DOM and DTM must have identical CRS, transform, and dimensions")
        if dom.crs is None or not dom.crs.is_projected:
            raise ValueError("DOM/DTM must use a projected CRS")

        width = math.ceil((dom.bounds.right - dom.bounds.left) / args.resolution)
        height = math.ceil((dom.bounds.top - dom.bounds.bottom) / args.resolution)
        transform = from_origin(
            dom.bounds.left, dom.bounds.top, args.resolution, args.resolution
        )
        profile = dom.profile.copy()
        profile.update(
            width=width,
            height=height,
            transform=transform,
            dtype="float32",
            nodata=np.nan,
            compress="deflate",
            predictor=3,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            bigtiff="IF_SAFER",
        )

        for source, output in ((dom, args.output_dom), (dtm, args.output_dtm)):
            output.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(output, "w", **profile) as destination:
                reproject(
                    source=rasterio.band(source, 1),
                    destination=rasterio.band(destination, 1),
                    src_transform=source.transform,
                    src_crs=source.crs,
                    src_nodata=source.nodata,
                    dst_transform=transform,
                    dst_crs=source.crs,
                    dst_nodata=np.nan,
                    resampling=method,
                    num_threads=2,
                )
                destination.set_band_description(
                    1, source.descriptions[0] or source.name
                )
            size_mb = output.stat().st_size / 1_000_000
            print(f"Wrote {output} ({width} x {height}, {size_mb:.1f} MB)")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        resample_pair(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
