#!/usr/bin/env python3
"""Load processed GeoJSON layers into PostgreSQL + PostGIS.

Reads data/processed/geojson/*.geojson and writes one table per layer
(schema 'public'), tagging rows with category/group for later unification
into infra_asset. Requires scripts/init_db.py to have been run.

Usage:
    python scripts/load_to_postgis.py            # all layers
    python scripts/load_to_postgis.py hospitals  # layer names, not categories
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd  # noqa: E402

from infrashield.config import PROCESSED_DATA_DIR, load_categories  # noqa: E402
from infrashield.db.loader import load_layer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("load_to_postgis")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("layers", nargs="*", help="layer names (default: all GeoJSON files)")
    args = parser.parse_args(argv)

    layer_to_group = {c.layer: c.group for c in load_categories()}
    geojson_dir = PROCESSED_DATA_DIR / "geojson"
    if not geojson_dir.exists():
        logger.error("No processed layers at %s -- run scripts/process_osm.py first", geojson_dir)
        return 1

    files = sorted(geojson_dir.glob("*.geojson"))
    if args.layers:
        wanted = set(args.layers)
        files = [f for f in files if f.stem in wanted]

    if not files:
        logger.error("No matching GeoJSON layers found")
        return 1

    loaded = 0
    for path in files:
        group = layer_to_group.get(path.stem, "other")
        gdf = gpd.read_file(path)
        rows = load_layer(gdf, layer=path.stem, group=group)
        loaded += rows

    print(f"\nLoaded {loaded:,} features into PostGIS ({len(files)} layers).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
