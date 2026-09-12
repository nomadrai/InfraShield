"""Infrastructure dependency graph.

Phase-1 scope: build a graph from *spatial co-location* between asset groups.
The graph answers "if this asset is impacted, what else is likely affected?"
via networkx single-source reachability with edge weights.

Dependency semantics: an edge source -> target means "source depends on
target", i.e. if target fails, source loses service. Phase-1 edges are
proximity-based proxies (a hospital "depends on" road/power assets near it);
the schema and API accept explicit, curated edges later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import geopandas as gpd
import networkx as nx

from infrashield.geo.processing import METRIC_CRS

logger = logging.getLogger(__name__)


@dataclass
class GraphBuildConfig:
    """Tuning knobs for the proximity-based graph build."""

    # group -> max distance (meters) at which a dependency edge is created.
    max_distance_m: dict[str, float] = field(
        default_factory=lambda: {
            "health_education": 150.0,  # hospitals/schools: assets serving them
            "transport": 100.0,
            "energy": 200.0,
            "communication": 250.0,
            "water": 150.0,
            "roads": 100.0,
        }
    )
    # Only facilities (points of service) get incoming dependency edges.
    facility_groups: tuple[str, ...] = ("health_education",)
    max_edges_per_facility: int = 12


def _class_of(row) -> str:
    tags = row.get("tags")
    if isinstance(tags, str) and tags:
        import json

        try:
            return str(json.loads(tags).get("amenity", ""))
        except json.JSONDecodeError:
            return ""
    return ""


def build_proximity_graph(
    assets: gpd.GeoDataFrame,
    config: GraphBuildConfig | None = None,
) -> nx.DiGraph:
    """Build a directed dependency graph from spatial proximity.

    ``assets`` must be in WGS84 and carry a ``group_name`` column. Edges are
    created facility-first: for each facility (e.g. a hospital), nearby
    assets from other groups become dependencies of that facility.
    """
    config = config or GraphBuildConfig()
    if assets.empty:
        return nx.DiGraph()

    metric = assets.to_crs(METRIC_CRS)
    graph = nx.DiGraph()

    for _, row in metric.iterrows():
        node_id = f"{row['osm_type']}/{row['osm_id']}"
        group = str(row.get("group_name", ""))
        graph.add_node(
            node_id,
            group=group,
            category=str(row.get("category", "")),
            name=row.get("name") or None,
        )

    facilities = metric[metric["group_name"].isin(config.facility_groups)]
    logger.info("Graph: %d facilities, %d assets total", len(facilities), len(metric))

    for _, facility in facilities.iterrows():
        facility_id = f"{facility['osm_type']}/{facility['osm_id']}"
        edges_added = 0
        for group, max_dist in config.max_distance_m.items():
            if group in config.facility_groups or edges_added >= config.max_edges_per_facility:
                continue
            candidates = metric[metric["group_name"] == group]
            if candidates.empty:
                continue
            distances = candidates.geometry.distance(facility.geometry)
            nearest = distances[distances <= max_dist].nsmallest(
                config.max_edges_per_facility - edges_added
            )
            for idx, dist in nearest.items():
                candidate = candidates.loc[idx]
                target_id = f"{candidate['osm_type']}/{candidate['osm_id']}"
                if target_id == facility_id:
                    continue
                weight = round(1.0 - min(dist / max_dist, 1.0), 3)  # closer = stronger
                if graph.has_edge(facility_id, target_id):
                    continue
                graph.add_edge(
                    facility_id,
                    target_id,
                    dep_type="spatial_proximity",
                    weight=weight,
                    distance_m=round(float(dist), 1),
                )
                edges_added += 1
            if edges_added >= config.max_edges_per_facility:
                break

    logger.info(
        "Graph built: %d nodes, %d dependency edges", graph.number_of_nodes(), graph.number_of_edges()
    )
    return graph


def cascade_from(graph: nx.DiGraph, failed_node: str) -> dict[str, float]:
    """Weighted cascade: impact score per dependent node.

    Edges point "dependent -> dependency", so when ``failed_node`` fails the
    impact propagates along *reverse* edges to everything that depends on it
    (directly or transitively), decaying by edge weight, until the remaining
    signal falls below the decay floor.
    """
    if failed_node not in graph:
        raise ValueError(f"Node {failed_node!r} not in graph")
    impacts: dict[str, float] = {}
    queue: list[tuple[str, float]] = [(failed_node, 1.0)]
    seen: set[str] = {failed_node}
    while queue:
        node, strength = queue.pop(0)
        for predecessor, _, data in graph.in_edges(node, data=True):
            propagated = strength * float(data.get("weight", 1.0))
            if propagated < 0.05:  # decay floor
                continue
            impacts[predecessor] = max(impacts.get(predecessor, 0.0), propagated)
            if predecessor not in seen:
                seen.add(predecessor)
                queue.append((predecessor, propagated))
    return {k: round(v, 3) for k, v in sorted(impacts.items(), key=lambda kv: -kv[1])}


def graph_summary(graph: nx.DiGraph) -> dict:
    """Quick stats for logs/reports."""
    groups: dict[str, int] = {}
    for _, data in graph.nodes(data=True):
        key = str(data.get("group", "unknown"))
        groups[key] = groups.get(key, 0) + 1
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "nodes_by_group": groups,
    }
