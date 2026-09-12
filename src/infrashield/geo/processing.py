"""Shared geometry cleaning / validation utilities.

Conventions used across InfraShield:

* WGS84 (EPSG:4326) is the storage and interchange CRS.
* UTM zone 43N (EPSG:32643) is the local metric CRS for the Mumbai AOI --
  used wherever lengths/areas/distances are computed.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from infrashield.config import StudyArea, load_study_area

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"
# UTM 43N covers Mumbai (lon 72.855..72.965 E); metric CRS for the AOI.
METRIC_CRS = "EPSG:32643"


def to_metric(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject to the local metric CRS (UTM 43N)."""
    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84)
    return gdf.to_crs(METRIC_CRS)


def clean_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Fix invalid geometries and drop empties; returns a new GeoDataFrame."""
    if gdf.empty:
        return gdf
    gdf = gdf.copy()
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        logger.info("Fixing %d invalid geometries", int(invalid.sum()))
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].make_valid()
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    return gdf


def clip_to_study_area(
    gdf: gpd.GeoDataFrame, study_area: StudyArea | None = None
) -> gpd.GeoDataFrame:
    """Clip features to the study-area bounding box.

    Uses ``GeoDataFrame.clip`` (not ``gpd.overlay``) so layers mixing points,
    lines, and polygons -- the norm for OSM categories -- clip correctly.
    ``keep_geom_type=True`` drops degenerate leftovers (e.g. boundary lines
    produced by polygon/bbox edge intersections).
    """
    study_area = study_area or load_study_area()
    if gdf.empty:
        return gdf
    bbox = box(study_area.west, study_area.south, study_area.east, study_area.north)
    return gdf.clip(mask=bbox, keep_geom_type=True)


def add_length_km(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add a ``length_km`` column (for line layers, in metric CRS)."""
    if gdf.empty or not (gdf.geom_type == "LineString").any():
        return gdf
    metric = to_metric(gdf)
    gdf = gdf.copy()
    gdf["length_km"] = (metric.geometry.length / 1000.0).round(3)
    return gdf


def layer_summary(gdf: gpd.GeoDataFrame) -> dict:
    """Small dict describing a processed layer (for logs and reports)."""
    if gdf.empty:
        return {"features": 0}
    summary: dict = {"features": int(len(gdf))}
    geom_types = gdf.geom_type.value_counts().to_dict()
    summary["geometry_types"] = {k: int(v) for k, v in geom_types.items()}
    if "length_km" in gdf.columns:
        summary["total_length_km"] = round(float(gdf["length_km"].sum()), 2)
    if "osm_type" in gdf.columns:
        summary["osm_types"] = {
            str(k): int(v) for k, v in gdf["osm_type"].value_counts().items()
        }
    return summary


def build_processing_summary(summaries: dict[str, dict]) -> pd.DataFrame:
    """Turn per-layer summaries into one DataFrame (used by process_osm.py)."""
    rows = []
    for layer, info in summaries.items():
        row = {"layer": layer, "features": info.get("features", 0)}
        if "total_length_km" in info:
            row["total_length_km"] = info["total_length_km"]
        rows.append(row)
    return pd.DataFrame(rows)
