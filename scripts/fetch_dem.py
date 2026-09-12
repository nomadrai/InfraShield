#!/usr/bin/env python3
"""Download the Copernicus GLO-30 DEM tile covering the study area.

Clips the 1-degree tile to the study-area bbox (plus a small buffer for
hydrological context) and stores it as data/raw/dem/dem_mumbai.tif.

Usage:
    python scripts/fetch_dem.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402
import rasterio  # noqa: E402
from rasterio.mask import mask  # noqa: E402
from shapely.geometry import box  # noqa: E402

from infrashield.config import DATA_DIR, load_study_area  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("fetch_dem")

# Copernicus GLO-30 public S3 bucket (COG, 1x1 degree tiles, ~30m).
S3 = "https://copernicus-dem-30m.s3.amazonaws.com"


def tile_name(lat: float, lon: float) -> tuple[str, str]:
    """Return (folder, file) for the GLO-30 tile whose SW corner is (lat, lon)."""
    lat0, lon0 = int(lat // 1), int(lon // 1)
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    base = f"Copernicus_DSM_COG_10_{ns}{abs(lat0):02d}_00_{ew}{abs(lon0):03d}_00_DEM"
    return base, f"{base}/{base}.tif"


def main() -> int:
    area = load_study_area()
    # Buffer the bbox slightly (~0.02 deg, ~2 km) so hydrological context and
    # edge effects are handled; the simulation itself stays AOI-faithful.
    west = area.west - 0.02
    east = area.east + 0.02
    south = area.south - 0.02
    north = area.north + 0.02

    base, url_path = tile_name(south, west)
    out_dir = DATA_DIR / "raw" / "dem"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dem_mumbai.tif"

    if out_path.exists():
        logger.info("DEM already present: %s (delete to re-download)", out_path)
        return 0

    url = f"{S3}/{url_path}"
    logger.info("Downloading %s ...", url)
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    tmp = out_path.with_suffix(".tmp.tif")
    tmp.write_bytes(resp.content)

    with rasterio.open(tmp) as src:
        bbox = box(west, south, east, north)
        clipped, transform = mask(src, [bbox.__geo_interface__], crop=True)
        profile = src.profile.copy()

    profile.update(
        height=clipped.shape[1],
        width=clipped.shape[2],
        transform=transform,
        compress="deflate",
    )
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(clipped)

    tmp.unlink(missing_ok=True)
    logger.info(
        "DEM saved: %s (%d x %d, res %.1f m)",
        out_path, clipped.shape[1], clipped.shape[2], transform.a,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
