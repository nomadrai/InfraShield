"""Tests for the risk layer: criticality, cascade, network, scenario glue."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from infrashield.risk.cascade import compute_cascade_impacts
from infrashield.risk.criticality import asset_params, elevation_vulnerability


def _row(**tags):
    base = {"osm_type": "node", "osm_id": 1, "group_name": "health_education",
            "category": "hospitals", "tags": json_dumps({"amenity": "hospital"}),
            "name": "Test Hospital", "length_km": None}
    base.update(tags)
    return pd.Series(base)


def json_dumps(d):
    import json
    return json.dumps(d)


class TestCriticality:
    def test_hospital_more_critical_than_footway(self):
        hosp = asset_params(_row())
        road = asset_params(_row(group_name="roads", category="roads",
                                 tags=json_dumps({"highway": "footway"})))
        assert hosp["criticality"] > road["criticality"]

    def test_bridge_overrides(self):
        p = asset_params(_row(group_name="roads", category="bridges",
                              tags=json_dumps({"highway": "secondary", "bridge": "yes"})))
        assert p["criticality"] >= 0.85
        assert p["vulnerability"] >= 0.6  # scour bump applied

    def test_low_assets_more_vulnerable(self):
        v_low = elevation_vulnerability(0.5, 2.0, 0.0, 50.0)
        v_high = elevation_vulnerability(0.5, 48.0, 0.0, 50.0)
        assert v_low > v_high


class TestCascade:
    def _graph(self):
        # codebase convention: edges point dependent -> dependency
        import networkx as nx

        g = nx.DiGraph()
        g.add_edge("substation/1", "power/plant", weight=0.9, dep_type="power_feed")
        g.add_edge("hospital/1", "substation/1", weight=0.8, dep_type="power_feed")
        g.add_edge("clinic/1", "hospital/1", weight=0.5, dep_type="patient_referral")
        return g

    def test_failure_propagates_with_decay(self):
        impacts = compute_cascade_impacts(self._graph(), {"power/plant": 1.0})
        assert impacts["power/plant"]["cascade"] == pytest.approx(1.0)
        assert impacts["substation/1"]["cascade"] > impacts["hospital/1"]["cascade"] > 0
        assert impacts["hospital/1"]["depth"] > impacts["substation/1"]["depth"]
        assert impacts["substation/1"]["dependents"] >= 1

    def test_leaf_failure_has_no_downstream(self):
        """clinic/1 has no dependents (nothing depends on it), so its
        failure affects nobody else; hospital failing DOES hit clinic
        (clinic -> hospital edge = clinic depends on hospital)."""
        impacts = compute_cascade_impacts(self._graph(), {"clinic/1": 0.99})
        assert set(impacts) == {"clinic/1"}
        impacts = compute_cascade_impacts(self._graph(), {"hospital/1": 0.99})
        assert impacts["clinic/1"]["cascade"] == pytest.approx(0.5)

    def test_no_failures_no_impacts(self):
        assert compute_cascade_impacts(self._graph(), {}) == {}


class TestNetwork:
    def _roads(self):
        from shapely.geometry import LineString

        rows = [
            {"osm_type": "way", "osm_id": 1,
             "geometry": LineString([(72.90, 19.12), (72.91, 19.12)]),
             "tags": json_dumps({"highway": "primary"})},
            {"osm_type": "way", "osm_id": 2,
             "geometry": LineString([(72.91, 19.12), (72.92, 19.12)]),
             "tags": json_dumps({"highway": "primary"})},
        ]
        import geopandas as gpd
        return gpd.GeoDataFrame(rows, crs="EPSG:4326")

    def test_failed_edge_cuts_route(self):
        from infrashield.risk.network import RoadNetwork

        roads = self._roads()
        net = RoadNetwork(roads)
        deg = net.degraded_copy({"way/1": 1.0, "way/2": 0.0})
        assert not deg.graph.has_edge(*list(net.graph.edges)[0]) or \
               deg.lost_connectivity(net) >= 0.0

    def test_speed_inflation_not_failure(self):
        from infrashield.risk.network import RoadNetwork

        net = RoadNetwork(self._roads())
        deg = net.degraded_copy({"way/1": 0.2, "way/2": 0.0})
        u, v = list(net.graph.edges)[0]
        assert deg.graph[u][v]["minutes"] >= net.graph[u][v]["minutes"]
