#!/usr/bin/env python3
"""Process raw Overpass JSON into clean GeoJSON layers + summaries.

Reads data/raw/osm/<category>.json, converts to GeoDataFrames, cleans and
clips to the study area, adds length for line layers, and writes:

* data/processed/geojson/<layer>.geojson  (WGS84, layer per category)
* data/processed/processing_summary.csv   (feature counts per layer)
* data/processed/layer_summary.json       (full per-layer stats)

Usage:
    python scripts/process_osm.py            # process everything present
    python scripts/process_osm.py hospitals  # one category
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd  # noqa: E402

from infrashield.config import (  # noqa: E402
    PROCESSED_DATA_DIR,
    RAW_DATA_DIR,
    get_category,
    load_categories,
    load_study_area,
)
from infrashield.geo.overpass_io import elements_to_geodataframe  # noqa: E402
from infrashield.geo.processing import (  # noqa: E402
    add_length_km,
    build_processing_summary,
    clean_geometries,
    clip_to_study_area,
    layer_summary,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("process_osm")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("categories", nargs="*", help="category names (default: all with raw data)")
    return parser.parse_args(argv)


def process_category(name: str, study_area) -> tuple[str, dict]:
    category = get_category(name)
    raw_path = RAW_DATA_DIR / "osm" / f"{category.name}.json"
    if not raw_path.exists():
        logger.warning("No raw data for %s (run scripts/fetch_osm.py first): %s", name, raw_path)
        return category.layer, {"features": 0, "skipped": "no raw data"}

    gdf = elements_to_geodataframe(raw_path)
    if gdf.empty:
        logger.warning("No geometries parsed for %s", name)
        return category.layer, {"features": 0, "skipped": "empty"}

    gdf = clean_geometries(gdf)
    gdf = clip_to_study_area(gdf, study_area)
    gdf = add_length_km(gdf)

    out_dir = PROCESSED_DATA_DIR / "geojson"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{category.layer}.geojson"
    gdf.to_file(out_path, driver="GeoJSON")

    summary = layer_summary(gdf)
    summary["raw_path"] = str(raw_path)
    summary["output"] = str(out_path)
    logger.info("%s: %s", category.layer, summary)
    return category.layer, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    study_area = load_study_area()
    categories = load_categories()

    if args.categories:
        names = args.categories
    else:
        names = [
            c.name
            for c in categories
            if (RAW_DATA_DIR / "osm" / f"{c.name}.json").exists()
        ]
        if not names:
            logger.error("No raw data under %s -- run scripts/fetch_osm.py first", RAW_DATA_DIR / "osm")
            return 1

    summaries: dict[str, dict] = {}
    for name in names:
        try:
            layer, summary = process_category(name, study_area)
            summaries[layer] = summary
        except Exception:
            logger.exception("Failed to process %s", name)

    if not summaries:
        logger.error("Nothing processed")
        return 1

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame = build_processing_summary(summaries)
    frame.to_csv(PROCESSED_DATA_DIR / "processing_summary.csv", index=False)
    (PROCESSED_DATA_DIR / "layer_summary.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )

    print("\n=== Processing summary ===")
    print(frame.to_string(index=False))
    print(f"\nGeoJSON layers -> {PROCESSED_DATA_DIR / 'geojson'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
