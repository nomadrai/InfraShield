"""OSM data acquisition via the Overpass API."""

from infrashield.osm.client import OverpassClient, fetch_category, fetch_all_categories

__all__ = ["OverpassClient", "fetch_category", "fetch_all_categories"]
