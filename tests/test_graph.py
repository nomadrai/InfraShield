"""Dependency graph tests: build, cascade, summary (offline)."""

from __future__ import annotations

import networkx as nx

from infrashield.graph.dependencies import (
    GraphBuildConfig,
    build_proximity_graph,
    cascade_from,
    graph_summary,
)

# Two facilities near road/energy assets; one facility far from everything.
ASSETS = {
    "elements": [
        # hospital (facility) near the road and the power line
        {"type": "node", "id": 1, "lat": 19.1200, "lon": 72.9000,
         "tags": {"amenity": "hospital", "name": "Near Hospital"}},
        # school (facility) near the road
        {"type": "node", "id": 2, "lat": 19.1205, "lon": 72.9005,
         "tags": {"amenity": "school"}},
        # road passing by both facilities
        {"type": "way", "id": 10, "tags": {"highway": "primary"},
         "geom": {"type": "LineString",
                  "coordinates": [[72.8990, 19.1190], [72.9015, 19.1215]]}},
        # power line near the hospital
        {"type": "way", "id": 11, "tags": {"power": "line"},
         "geom": {"type": "LineString",
                  "coordinates": [[72.9005, 19.1205], [72.9010, 19.1210]]}},
        # distant facility: ~2+ km away from all infrastructure above
        {"type": "node", "id": 3, "lat": 19.1450, "lon": 72.9500,
         "tags": {"amenity": "clinic", "name": "Far Clinic"}},
    ]
}


def _assets_gdf():
    from infrashield.geo.overpass_io import elements_to_geodataframe

    gdf = elements_to_geodataframe(ASSETS, keep_keys=["name"])
    gdf["group_name"] = ["health_education", "health_education", "roads", "energy",
                         "health_education"]
    gdf["category"] = ["hospitals", "schools", "major_roads", "power", "hospitals"]
    return gdf


def test_build_proximity_graph_connects_facilities_to_nearby_assets() -> None:
    graph = build_proximity_graph(_assets_gdf(), GraphBuildConfig())
    assert graph.number_of_nodes() == 5
    # Hospital (node 1) should depend on the nearby road (10) and power line (11).
    assert graph.has_edge("node/1", "way/10")
    assert graph.has_edge("node/1", "way/11")
    # School depends on the road too.
    assert graph.has_edge("node/2", "way/10")


def test_graph_edges_are_weighted_and_typed() -> None:
    graph = build_proximity_graph(_assets_gdf(), GraphBuildConfig())
    data = graph.get_edge_data("node/1", "way/10")
    assert data is not None
    assert data["dep_type"] == "spatial_proximity"
    assert 0.0 < data["weight"] <= 1.0
    assert data["distance_m"] >= 0


def test_cascade_from_road_impacts_downstream_facilities() -> None:
    graph = build_proximity_graph(_assets_gdf(), GraphBuildConfig())
    impacts = cascade_from(graph, "way/10")
    assert "node/1" in impacts and "node/2" in impacts
    assert "node/3" not in impacts          # far clinic unaffected
    assert impacts["node/1"] > 0


def test_cascade_unknown_node_raises() -> None:
    graph = build_proximity_graph(_assets_gdf(), GraphBuildConfig())
    try:
        cascade_from(graph, "node/999999")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_graph_summary_counts_groups() -> None:
    graph = build_proximity_graph(_assets_gdf(), GraphBuildConfig())
    summary = graph_summary(graph)
    assert summary["nodes"] == 5
    assert summary["nodes_by_group"]["health_education"] == 3
    assert summary["edges"] >= 3


def test_empty_assets_return_empty_graph() -> None:
    import geopandas as gpd

    empty = gpd.GeoDataFrame(columns=["osm_type", "osm_id", "group_name", "geometry"],
                             geometry="geometry", crs="EPSG:4326")
    graph = build_proximity_graph(empty)
    assert isinstance(graph, nx.DiGraph)
    assert graph.number_of_nodes() == 0
