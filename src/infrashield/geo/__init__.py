"""Geospatial processing utilities (GeoPandas / Shapely)."""

from infrashield.geo.overpass_io import elements_to_geodataframe
from infrashield.geo.processing import (
    build_processing_summary,
    clean_geometries,
    clip_to_study_area,
    filter_category,
    layer_summary,
)

__all__ = [
    "elements_to_geodataframe",
    "build_processing_summary",
    "clean_geometries",
    "clip_to_study_area",
    "filter_category",
    "layer_summary",
]
