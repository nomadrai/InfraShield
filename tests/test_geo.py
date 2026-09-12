"""Geo utility tests: Overpass parsing, cleaning, clipping, summaries."""

from __future__ import annotations

import geopandas as gpd
import pytest

from infrashield.geo.overpass_io import elements_to_geodataframe
from infrashield.geo.processing import (
    add_length_km,
    clean_geometries,
    clip_to_study_area,
    layer_summary,
    to_metric,
)


def test_elements_to_geodataframe_parses_nodes_and_ways(overpass_payload: dict) -> None:
    gdf = elements_to_geodataframe(overpass_payload)
    assert len(gdf) == 4
    assert set(gdf["osm_type"]) == {"node", "way"}
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    # tags kept as JSON string column
    assert "tags" in gdf.columns
    assert "Test Hospital" in gdf["tags"].iloc[0]
    # ways parsed from the {lat,lon} point-list wire format
    ways = gdf[gdf["osm_type"] == "way"]
    assert (ways.geom_type == "LineString").all()


def test_elements_to_geodataframe_keep_keys(overpass_payload: dict) -> None:
    gdf = elements_to_geodataframe(overpass_payload, keep_keys=["name", "amenity"])
    assert "name" in gdf.columns
    assert "tags" not in gdf.columns
    names = set(gdf["name"].dropna())
    assert "Test Hospital" in names


def test_elements_to_geodataframe_center_points(overpass_payload: dict) -> None:
    gdf = elements_to_geodataframe(overpass_payload, geometry="center")
    assert (gdf.geom_type == "Point").all()


def test_closed_way_with_area_tags_becomes_polygon(closed_way_payload: dict) -> None:
    gdf = elements_to_geodataframe(closed_way_payload)
    hospital = gdf[gdf["osm_id"] == 3001].iloc[0]
    assert hospital.geometry.geom_type == "Polygon"


def test_relation_assembles_multipolygon_with_hole(closed_way_payload: dict) -> None:
    gdf = elements_to_geodataframe(closed_way_payload)
    lake = gdf[gdf["osm_id"] == 4001].iloc[0]
    assert lake.geometry.geom_type in ("Polygon", "MultiPolygon")
    # inner ring subtracted -> area smaller than the outer ring alone
    outer_only = 0.01 * 0.01  # 0.01 deg x 0.01 deg
    assert lake.geometry.area < outer_only


def test_empty_payload_returns_empty_gdf() -> None:
    gdf = elements_to_geodataframe({"elements": []})
    assert isinstance(gdf, gpd.GeoDataFrame)
    assert len(gdf) == 0


def test_deduplicates_same_element(overpass_payload: dict) -> None:
    duplicated = {"elements": overpass_payload["elements"] + overpass_payload["elements"]}
    gdf = elements_to_geodataframe(duplicated)
    assert len(gdf) == 4


def test_clean_geometries_keeps_valid(study_area) -> None:
    gdf = elements_to_geodataframe({"elements": [
        {"type": "way", "id": 1, "tags": {}, "geometry": [
            {"lat": 19.09, "lon": 72.86}, {"lat": 19.10, "lon": 72.87},
        ]},
    ]})
    cleaned = clean_geometries(gdf)
    assert len(cleaned) == 1


@pytest.mark.parametrize(
    "geom_coords,inside",
    [
        ([[19.09, 72.86], [19.10, 72.87]], True),   # fully inside AOI
        ([[18.00, 72.00], [18.10, 72.10]], False),  # far away
    ],
)
def test_clip_to_study_area_filters_lines(study_area, geom_coords, inside) -> None:
    gdf = elements_to_geodataframe({"elements": [
        {"type": "way", "id": 1, "tags": {}, "geometry": [
            {"lat": lat, "lon": lon} for lat, lon in geom_coords
        ]},
    ]})
    clipped = clip_to_study_area(gdf, study_area)
    if inside:
        assert len(clipped) == 1
    else:
        assert len(clipped) == 0


def test_add_length_km_on_lines(overpass_payload: dict) -> None:
    gdf = elements_to_geodataframe(overpass_payload)
    gdf = add_length_km(gdf)
    assert "length_km" in gdf.columns
    lines = gdf[gdf.geom_type == "LineString"]
    assert (lines["length_km"] > 0).all()


def test_to_metric_changes_crs(overpass_payload: dict) -> None:
    gdf = to_metric(elements_to_geodataframe(overpass_payload))
    assert gdf.crs.to_epsg() == 32643


def test_layer_summary_shape(overpass_payload: dict) -> None:
    gdf = add_length_km(elements_to_geodataframe(overpass_payload))
    summary = layer_summary(gdf)
    assert summary["features"] == 4
    assert "LineString" in summary["geometry_types"]
    assert summary["total_length_km"] > 0
