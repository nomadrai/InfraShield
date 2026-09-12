#!/usr/bin/env python3
"""Fetch Mumbai OSM data for all (or selected) InfraShield categories.

Usage:
    python scripts/fetch_osm.py                 # all categories
    python scripts/fetch_osm.py hospitals roads # subset
    python scripts/fetch_osm.py --list          # show categories
    python scripts/fetch_osm.py --no-cache      # bypass the byte cache

Raw JSON lands in data/raw/osm/<category>.json; a byte cache under
data/cache/overpass makes repeat runs cheap and offline-friendly.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from infrashield.config import load_categories  # noqa: E402
from infrashield.osm.client import OverpassClient, fetch_all_categories  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("fetch_osm")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("categories", nargs="*", help="category names (default: all)")
    parser.add_argument("--list", action="store_true", help="list categories and exit")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    categories = load_categories()

    if args.list:
        for cat in categories:
            print(f"{cat.name:15s} {cat.group:18s} {cat.description}")
        return 0

    if args.categories:
        known = {c.name for c in categories}
        unknown = [name for name in args.categories if name not in known]
        if unknown:
            logger.error("Unknown categories: %s (known: %s)", ", ".join(unknown), ", ".join(sorted(known)))
            return 2
        categories = [c for c in categories if c.name in set(args.categories)]

    client = OverpassClient()
    if args.no_cache:
        import shutil

        shutil.rmtree(client.cache_dir, ignore_errors=True)
        logger.info("Cleared Overpass byte cache at %s", client.cache_dir)

    logger.info(
        "Fetching %d categories in bbox %s from %s",
        len(categories), client.study_area.bbox_overpass, client.endpoint,
    )
    results = fetch_all_categories(categories)

    print("\n=== Fetch summary ===")
    for res in results:
        source = "cache" if res.from_cache else "network"
        print(f"  {res.category:15s} {res.element_count:>7,} elements  [{source}]  -> {res.raw_path}")
    total = sum(r.element_count for r in results)
    print(f"  {'TOTAL':15s} {total:>7,} elements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
