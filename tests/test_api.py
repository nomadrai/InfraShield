"""API contract tests - offline via stubbed engine state."""

from __future__ import annotations

import numpy as np
import pytest

fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from infrashield import api as api_mod
    from infrashield.hazard.terrain import TerrainData

    h, w = 12, 12
    dem = np.full((h, w), 20.0)
    transform = (0.001, 0.0, 72.85, 0.0, -0.001, 19.18)
    terrain = TerrainData(
        dem=dem, filled=dem, fill_depth=np.zeros_like(dem),
        flow_dir=np.full(h * w, -1, dtype=np.int64), flow_acc=np.ones((h, w)),
        slope=np.zeros_like(dem), cell_area_m2=111.0**2,
        channel_mask=np.zeros((h, w), dtype=bool), depressions=[],
        transform=transform, crs="EPSG:4326", height=h, width=w,
    )

    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame({
        "osm_type": ["node"], "osm_id": [1], "name": ["T"],
        "category": ["hospitals"], "group_name": ["health_education"],
        "tags": ['{"amenity": "hospital"}'],
        "length_km": [None], "geometry": [Point(72.9, 19.1)],
    }, crs="EPSG:4326")

    import networkx as nx
    g = nx.DiGraph()
    g.add_node("node/1", group="health_education", name="T")

    # stub the process-wide state (skip DEM preprocessing)
    monkeypatch.setattr(api_mod, "_get_state",
                        lambda: {"terrain": terrain, "layers": {"hospitals": gdf},
                                 "dep_graph": g})
    return TestClient(api_mod.app)


def test_health(client):
    r = client.get("/api/health").json()
    assert r["ok"] is True
    assert r["state"] == "ready"  # lazy mode: no startup warmup in tests


def test_meta_shape(client):
    m = client.get("/api/meta").json()
    assert set(m["hazards"]) == {"flood", "heat", "cyclone", "landslide"}
    assert m["study_area"]["bbox"] == [72.855, 19.085, 72.965, 19.175]


def test_layers_geojson(client):
    data = client.get("/api/layers").json()
    f = data["hospitals"]["features"][0]
    assert f["id"] == "node/1"
    assert f["geometry"]["type"] == "Point"


def test_simulate_flood_contract(client):
    r = client.post("/api/simulate", json={"hazard_type": "flood",
                                           "params": {"rainfall_mm": 300}})
    assert r.status_code == 200
    d = r.json()
    assert d["hazard_type"] == "flood"
    assert len(d["stages"]) == 8
    assert d["stages"][0]["png"] is not None or d["stages"][0]["meta"]["max"] == 0
    assert isinstance(d["assets"], list)
    assert d["assets"][0]["priority"] >= d["assets"][-1]["priority"]  # sorted
    assert "cascade_edges" in d


def test_simulate_validates_params(client):
    r = client.post("/api/simulate", json={"hazard_type": "nope"})
    assert r.status_code == 422  # pydantic pattern
    r = client.post("/api/simulate", json={"hazard_type": "flood",
                                           "params": {"rainfall_mm": "abc"}})
    assert r.status_code == 400


def test_simulation_history_roundtrip(client):
    client.post("/api/simulate", json={"hazard_type": "heat", "params": {}})
    lst = client.get("/api/simulations").json()
    assert isinstance(lst, list) and len(lst) >= 1
    sim_id = lst[0]["sim_id"]
    det = client.get(f"/api/simulations/{sim_id}").json()
    assert det["sim_id"] == sim_id
    assert client.get("/api/simulations/missing-id").status_code == 404
