"""Road-network impact analysis.

Builds a routable graph from OSM road geometries (vertex-sharing
connectivity), degrades edges by their asset risk, and measures:

* **Lost connectivity**: edges whose risk exceeds a fail threshold are
  removed; weakly-connected components that shrink lose internal routes.
* **Detour impact**: for failed edges, the travel-time increase between
  their endpoints through the remaining network (None = disconnected).
* **Accessibility**: travel-time from each populated minor-road centroid
  to its nearest critical facility (hospital/fire/police) before vs after;
  increases become the facility's ``network_impact``.

All from the actual road geometry + simulated risk — nothing scripted.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

import numpy as np

logger = logging.getLogger(__name__)

FAIL_RISK = 0.35          # asset risk above which a road edge is unusable
DEGRADE_EXP = 2.0         # travel-time penalty grows with risk^DEGRADE_EXP
SPEED_KMH = {"motorway": 80, "trunk": 65, "primary": 45, "secondary": 35,
             "tertiary": 30, "residential": 25, "service": 15, "unclassified": 25}


class RoadNetwork:
    """Vertex-sharing graph over road geometries with hazard degradation."""

    def __init__(self, roads_gdf, fail_risk: float = FAIL_RISK) -> None:
        self.fail_risk = fail_risk
        self.graph = __import__("networkx").Graph()
        self.edge_asset: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._build(roads_gdf)

    def _speed(self, highway: str) -> float:
        return float(SPEED_KMH.get(highway, 25.0))

    def _build(self, gdf) -> None:
        import networkx as nx

        self.graph = nx.Graph()
        self.edge_asset = defaultdict(list)
        h, w = 0, 0  # unused; kept for clarity
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            if geom.geom_type == "MultiLineString":
                parts = list(geom.geoms)
            elif geom.geom_type == "LineString":
                parts = [geom]
            else:
                continue
            asset_id = f"{row['osm_type']}/{row['osm_id']}"
            tags = {}
            raw = row.get("tags")
            if isinstance(raw, str) and raw:
                import json
                try:
                    tags = json.loads(raw)
                except json.JSONDecodeError:
                    tags = {}
            highway = tags.get("highway", "residential")
            speed = self._speed(highway)
            for part in parts:
                coords = list(part.coords)
                if len(coords) < 2:
                    continue
                for (lon1, lat1), (lon2, lat2) in zip(coords[:-1], coords[1:]):
                    u = (round(lon1, 6), round(lat1, 6))
                    v = (round(lon2, 6), round(lat2, 6))
                    if u == v:
                        continue
                    # haversine length
                    dlon = math.radians(lon2 - lon1)
                    dlat = math.radians(lat2 - lat1)
                    a_ = (math.sin(dlat / 2) ** 2
                          + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
                          * math.sin(dlon / 2) ** 2)
                    dist_km = 2 * 6371.0 * math.asin(math.sqrt(a_))
                    minutes = dist_km / speed * 60.0
                    if self.graph.has_edge(u, v):
                        # keep the faster representation
                        if minutes < self.graph[u][v]["minutes"]:
                            self.graph[u][v].update(minutes=minutes, dist_km=dist_km,
                                                    highway=highway)
                        if asset_id not in self.edge_asset[(u, v)]:
                            self.edge_asset[(u, v)].append(asset_id)
                        continue
                    self.graph.add_edge(u, v, minutes=minutes, dist_km=dist_km,
                                        highway=highway)
                    self.edge_asset[(u, v)].append(asset_id)

    # ------------------------------------------------------------------ #
    # Degradation
    # ------------------------------------------------------------------ #

    def degraded_copy(self, risk_by_asset: dict[str, float]) -> "RoadNetwork":
        """A copy with travel times inflated/edges removed per asset risk.

        ``risk_by_asset`` values should be an effective *usability loss* for
        the edge (the scenario stage passes max(asset risk, exposure) so a
        deeply flooded minor road is closed even though its consequence
        weighted risk is moderate — criticality belongs to impact, not to
        passability).
        """
        import networkx as nx

        clone = RoadNetwork.__new__(RoadNetwork)
        clone.fail_risk = self.fail_risk
        clone.graph = self.graph.copy()
        clone.edge_asset = defaultdict(list,
                                       {k: list(v) for k, v in self.edge_asset.items()})
        for (u, v), assets in self.edge_asset.items():
            if not clone.graph.has_edge(u, v):
                continue
            risk = max(risk_by_asset.get(a, 0.0) for a in assets)
            if risk >= clone.fail_risk:
                clone.graph.remove_edge(u, v)
            elif risk > 0.05:
                clone.graph[u][v]["minutes"] *= 1.0 + DEGRADE_EXP * risk ** 2
        return clone

    # ------------------------------------------------------------------ #
    # Impact metrics
    # ------------------------------------------------------------------ #

    def lost_connectivity(self, baseline: "RoadNetwork") -> float:
        """Fraction of edges unreachable from the main component vs baseline."""
        import networkx as nx

        if baseline.graph.number_of_nodes() == 0:
            return 0.0
        base_main = max(nx.connected_components(baseline.graph), key=len)
        if self.graph.number_of_nodes() == 0:
            return 1.0
        main = max(nx.connected_components(self.graph), key=len)
        lost = len(base_main - main)
        return lost / max(len(base_main), 1)

    def detour_minutes(
        self, baseline: "RoadNetwork", max_pairs: int = 400,
    ) -> dict[str, float | None]:
        """Per failed edge: extra minutes between its endpoints (None = cut).

        Bounded to ``max_pairs`` Dijkstra runs (worst-case edges are sampled
        first since they matter most) so a severe scenario cannot stall the
        request; the bounded sample feeds both per-asset network impact and
        the summary statistics.
        """
        import networkx as nx

        failed = []
        for (u, v) in self.edge_asset:
            if baseline.graph.has_edge(u, v) and not self.graph.has_edge(u, v):
                base_min = baseline.graph[u][v]["minutes"]
                failed.append((base_min, u, v))
        failed.sort(reverse=True)  # busiest roads first
        out: dict[str, float | None] = {}
        for base_min, u, v in failed[:max_pairs]:
            try:
                new_t = nx.shortest_path_length(self.graph, u, v, weight="minutes")
                out[f"{u}|{v}"] = max(0.0, new_t - base_min)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                out[f"{u}|{v}"] = None
        return out

    def _nearest_node(self, point: tuple[float, float]) -> tuple | None:
        if not self.graph.number_of_nodes():
            return None
        nodes = list(self.graph.nodes)
        arr = np.array(nodes)
        p = np.array(point)
        return tuple(arr[int(np.argmin(((arr - p) ** 2).sum(axis=1)))])

    def accessibility_impact(
        self, baseline: "RoadNetwork", facilities: list[tuple[float, float]],
        demand: list[tuple[float, float]], isolation_penalty_min: float = 45.0,
    ) -> tuple[float, float]:
        """Travel-time increase (minutes) from demand points to the nearest
        critical facility, degraded network vs baseline.

        Implemented as *multi-source Dijkstra from all facility nodes* on
        each network (2 passes total), so it scales to city-sized graphs.
        Returns (mean_increase_minutes, isolated_demand_fraction).
        """
        import networkx as nx

        if not facilities or not demand:
            return 0.0, 0.0
        fac_nodes = [self._nearest_node(f) for f in facilities]
        fac_nodes = [f for f in fac_nodes if f is not None and f in self.graph]
        if not fac_nodes:
            return 0.0, 0.0

        def min_time(graph, targets) -> dict:
            try:
                return dict(nx.multi_source_dijkstra_path_length(
                    graph, targets, weight="minutes"))
            except ValueError:  # no targets on this graph
                return {}

        base_times = min_time(baseline.graph, fac_nodes)
        new_times = min_time(self.graph, fac_nodes)

        increases: list[float] = []
        isolated = 0
        for d in demand:
            dn = self._nearest_node(d)
            if dn is None or dn not in base_times:
                continue
            base_t = base_times[dn]
            new_t = new_times.get(dn)
            if new_t is None:
                isolated += 1
                increases.append(isolation_penalty_min)
            elif new_t > base_t + 0.5:
                increases.append(new_t - base_t)
        if not increases:
            return 0.0, 0.0
        return float(np.mean(increases)), isolated / max(len(increases), 1)

    def node_risk_load(self, risk_by_asset: dict[str, float]) -> dict[tuple, float]:
        """Total failed-asset risk carried by each graph node (for hotspots)."""
        loads: dict[tuple, float] = defaultdict(float)
        for (u, v), assets in self.edge_asset.items():
            for a in assets:
                r = risk_by_asset.get(a, 0.0)
                if r >= self.fail_risk:
                    loads[u] += r
                    loads[v] += r
        return dict(loads)
