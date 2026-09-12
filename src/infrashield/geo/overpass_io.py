"""Convert Overpass JSON responses into GeoPandas GeoDataFrames.

Supports the two geometry encodings seen across Overpass deployments:

* GeoJSON-style ``geom`` dict (``{"type": "LineString", ...}``);
* the common ``out body geom`` encoding: ways carry ``geometry`` as a list of
  ``{"lat": .., "lon": ..}`` points, nodes carry top-level ``lat``/``lon``.

Relations are assembled into (Multi)Polygons from their member ways
(outer rings via polygonize, inner rings subtracted). Closed ways whose tags
imply an area (building, amenity, landuse, ...) become Polygons; other closed
or open ways stay LineStrings.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union

from infrashield.config import RAW_DATA_DIR

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"

# Way tags that mark a closed way as an area (polygon) rather than a line.
AREA_TAG_KEYS = ("building", "landuse", "leisure", "amenity", "natural")


def _coords_from_point_list(points: list) -> list[tuple[float, float]]:
    """Convert Overpass point objects [{lat, lon}, ...] to coord tuples."""
    return [
        (p["lon"], p["lat"])
        for p in points
        if p and "lat" in p and "lon" in p
    ]


def _tags_imply_area(tags: dict) -> bool:
    if tags.get("area") == "yes":
        return True
    return any(key in tags for key in AREA_TAG_KEYS)


def _way_geometry(element: dict) -> BaseGeometry | None:
    coords = _coords_from_point_list(element.get("geometry") or [])
    if len(coords) < 2:
        return None
    line = LineString(coords)
    if line.is_closed and _tags_imply_area(element.get("tags", {})):
        try:
            return Polygon(line)
        except ValueError:  # degenerate ring
            return line
    return line


def _relation_geometry(element: dict) -> BaseGeometry | None:
    """Assemble a relation (multipolygon-style) from member way geometries."""
    outers: list[LineString] = []
    inners: list[LineString] = []
    for member in element.get("members", []):
        if member.get("type") != "way":
            continue
        coords = _coords_from_point_list(member.get("geometry") or [])
        if len(coords) < 2:
            continue
        line = LineString(coords)
        (inners if member.get("role") == "inner" else outers).append(line)

    if not outers:
        return None
    try:
        merged = linemerge(outers)
        polygons = list(polygonize(merged))
        if not polygons:
            return None
        result: BaseGeometry = unary_union(polygons)
        if inners:
            inner_rings = list(polygonize(linemerge(inners)))
            if inner_rings:
                result = result.difference(unary_union(inner_rings))
        return result
    except (ValueError, TypeError, AssertionError) as exc:
        logger.debug("Relation %s: could not assemble geometry (%s)", element.get("id"), exc)
        return None


def _geometry_of(element: dict) -> BaseGeometry | None:
    """Extract a Shapely geometry from one Overpass element."""
    geom = element.get("geom")
    if isinstance(geom, dict) and "type" in geom:  # GeoJSON-style dict
        try:
            return shape(geom)
        except (ValueError, KeyError, AttributeError):
            return None

    if element.get("type") == "way":
        return _way_geometry(element)
    if element.get("type") == "relation":
        return _relation_geometry(element)

    if "lat" in element and "lon" in element:  # node
        return Point(element["lon"], element["lat"])
    return None


def _elements_from_payload(payload: dict | Path | str) -> list[dict]:
    if isinstance(payload, dict):
        return payload.get("elements", [])
    path = Path(payload)
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh).get("elements", [])
    raise FileNotFoundError(f"Overpass payload not found: {payload}")


def elements_to_geodataframe(
    payload: dict | Path | str,
    geometry: str = "geom",
    keep_keys: list[str] | None = None,
) -> gpd.GeoDataFrame:
    """Build a GeoDataFrame from Overpass elements.

    geometry: "geom" -> the element's natural geometry (points for nodes,
    lines/polygons for ways, assembled polygons for relations).
    "center" -> centroid points only (useful for polygonal POIs).
    """
    elements = _elements_from_payload(payload)

    records: list[dict] = []
    for element in elements:
        geom = _geometry_of(element)
        if geom is None:
            continue
        if geometry == "center":
            geom = geom.centroid
        tags = element.get("tags", {})
        row = {
            "osm_type": element.get("type"),
            "osm_id": element.get("id"),
            "geometry": geom,
        }
        if keep_keys:
            for key in keep_keys:
                if key in tags:
                    row[key] = tags[key]
        else:
            row["tags"] = json.dumps(tags, ensure_ascii=False)
        records.append(row)

    if not records:
        return gpd.GeoDataFrame(
            {"osm_type": pd.Series(dtype="object"), "osm_id": pd.Series(dtype="int64")},
            geometry=[],
            crs=WGS84,
        )

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=WGS84)
    # De-duplicate elements appearing under several categories.
    return gdf.drop_duplicates(subset=["osm_type", "osm_id"])
