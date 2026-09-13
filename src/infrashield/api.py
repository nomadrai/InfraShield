"""InfraShield API: the dashboard's only backend.

Endpoints (all JSON, CORS-open for local dev):

* ``GET  /api/meta``        study area bbox, hazard catalogue, scenario
                            parameter schema, layer groups
* ``GET  /api/layers``      GeoJSON layers for the map
* ``POST /api/simulate``    run a scenario through the real engine and
                            return grids + per-asset assessments + summary
* ``GET  /api/simulations`` list previously saved runs
* ``GET  /api/simulations/{sim_id}`` fetch one saved run

The simulate endpoint is synchronous (~10-15 s per scenario) and its whole
payload is derived from the DEM-driven hazard simulation, exposure sampling
at asset geometries, the road-graph network analysis and the dependency
cascade - there are no scripted results anywhere.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from infrashield.config import load_study_area
from infrashield.risk.scenario import run_scenario
from infrashield.store import (
    list_simulations,
    load_layers,
    load_simulation,
    load_terrain,
    save_simulation,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="InfraShield API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Process-wide singletons: DEM preprocessing takes seconds, layers take
# ~0.5 s - load them once and reuse across requests.
_state: dict = {}


def _get_state() -> dict:
    if not _state:
        terrain = load_terrain()
        layers = load_layers()
        from infrashield.store import build_dependency_graph

        dep_graph = build_dependency_graph(layers)
        _state.update(terrain=terrain, layers=layers, dep_graph=dep_graph)
    return _state


HAZARDS = {
    "flood": {
        "label": "Urban flood",
        "params": {
            "rainfall_mm": {"label": "Rainfall (mm)", "min": 50, "max": 900, "step": 10, "default": 300},
            "duration_h": {"label": "Duration (h)", "min": 1, "max": 72, "step": 1, "default": 24},
        },
        "stages": "Topographic water propagation (DEM fill -> D8 -> depression storage -> channel overflow)",
    },
    "heat": {
        "label": "Extreme heat",
        "params": {
            "anomaly_c": {"label": "Heatwave anomaly (°C)", "min": 2, "max": 15, "step": 0.5, "default": 8},
        },
        "stages": "Static field: lapse rate + urban heat island + water cooling",
    },
    "cyclone": {
        "label": "Cyclone",
        "params": {
            "category": {"label": "Category (1-5)", "min": 1, "max": 5, "step": 1, "default": 3},
        },
        "stages": "Parametric wind field + DEM-driven storm-surge inundation",
    },
}

GROUP_LABELS = {
    "health_education": "Health & education",
    "energy": "Power & energy",
    "communication": "Communication",
    "transport": "Rail & air transport",
    "roads": "Roads & bridges",
    "water": "Water",
}


class SimulateRequest(BaseModel):
    hazard_type: str = Field(pattern="^(flood|heat|cyclone)$")
    params: dict = Field(default_factory=dict)


def _cascade_edges(dep_graph, assets: list[dict]) -> list[dict]:
    """Dependency edges among *affected* assets, for chain visualisation.

    An edge qualifies when either endpoint is materially affected (exposed,
    failed, or carrying cascade impact) - the raw graph has ~5k edges across
    12k nodes, far too noisy to draw wholesale.
    """
    affected = {
        a["asset_id"] for a in assets
        if a["exposure"] > 0.05 or a["risk"] > 0.1 or a["cascade_impact"] > 0
    }
    out = []
    for u, v, data in dep_graph.edges(data=True):
        if u in affected or v in affected:
            out.append({
                "source": u, "target": v,
                "dep_type": data.get("dep_type", ""),
                "weight": round(float(data.get("weight", 1.0)), 3),
            })
    out.sort(key=lambda e: -e["weight"])
    return out[:4000]


def _grid_png(grid: np.ndarray) -> str | None:
    """Encode a float grid as a base64 16-bit greyscale PNG for the map.

    The frontend decodes pixel values back to 0..1 (grid_max normalised) and
    renders them through its own colour ramp - far smaller than GeoJSON and
    pixel-exact with what the engine computed.
    """
    if grid is None or grid.size == 0:
        return None
    import io

    from PIL import Image

    finite = np.where(np.isfinite(grid), grid, 0.0)
    vmax = float(finite.max())
    if vmax <= 0:
        return None
    norm = np.clip(finite / vmax, 0.0, 1.0)
    img = Image.fromarray((norm * 65535.0).astype(np.uint16))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _grid_meta(grid: np.ndarray) -> dict:
    finite = grid[np.isfinite(grid)]
    return {
        "max": round(float(finite.max()), 3) if finite.size else 0.0,
        "mean": round(float(finite.mean()), 3) if finite.size else 0.0,
    }


def _bbox(layers: dict) -> list[float]:
    xmin = ymin = 1e9
    xmax = ymax = -1e9
    for gdf in layers.values():
        if gdf.empty:
            continue
        b = gdf.total_bounds
        xmin, ymin, xmax, ymax = min(xmin, b[0]), min(ymin, b[1]), max(xmax, b[2]), max(ymax, b[3])
    return [round(xmin, 5), round(ymin, 5), round(xmax, 5), round(ymax, 5)]


@app.get("/api/meta")
def meta() -> dict:
    state = _get_state()
    t = state["terrain"]
    dem = t.dem[np.isfinite(t.dem)]
    area = load_study_area()
    return {
        "study_area": {
            "name": "Mumbai AOI",
            "bbox": [area.west, area.south, area.east, area.north],
            "dem_bbox": [t.transform[2], t.transform[5] + t.height * t.transform[4],
                         t.transform[2] + t.width * t.transform[0], t.transform[5]],
            "grid": {"height": t.height, "width": t.width,
                     "cell_size_m": round(t.cell_area_m2 ** 0.5, 1)},
            "elevation_range_m": [round(float(dem.min()), 1), round(float(dem.max()), 1)],
        },
        "hazards": HAZARDS,
        "groups": GROUP_LABELS,
        "counts": {name: len(gdf) for name, gdf in state["layers"].items()},
    }


@app.get("/api/layers")
def layers() -> dict:
    """Asset layers as GeoJSON FeatureCollections, keyed by group."""
    state = _get_state()
    out: dict[str, dict] = {}
    for name, gdf in state["layers"].items():
        if name in ("waterways", "residential"):
            # context layers: waterways drawn from OSM directly by the
            # frontend if needed; residential polygons only for stats
            continue
        props = ["osm_type", "osm_id", "name", "category", "group_name"]
        cols = [c for c in props if c in gdf.columns]
        gj = gdf[cols + ["geometry"]].to_crs("EPSG:4326").__geo_interface__
        out[name] = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": f"{f['properties'].get('osm_type')}/{f['properties'].get('osm_id')}",
                    "geometry": f["geometry"],
                    "properties": f["properties"],
                }
                for f in gj["features"]
            ],
        }
    return out


@app.post("/api/simulate")
def simulate(req: SimulateRequest) -> dict:
    state = _get_state()
    terrain = state["terrain"]
    hazard = HAZARDS.get(req.hazard_type)
    if hazard is None:
        raise HTTPException(400, f"unknown hazard_type {req.hazard_type!r}")

    # clamp params to the declared ranges (and inject defaults)
    params: dict = {}
    for key, spec in hazard["params"].items():
        raw = req.params.get(key, spec["default"])
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"param {key!r} must be numeric")
        params[key] = float(min(max(raw, spec["min"]), spec["max"]))

    t0 = time.time()
    result = run_scenario(
        state["terrain"], state["layers"], state["dep_graph"],
        req.hazard_type, params,
    )
    elapsed = round(time.time() - t0, 1)

    # hazard grids -> base64 PNG overlays (per stage for flood animation)
    stage_grids = []
    for i, (label, grid) in enumerate(zip(result.stage_labels, result.stages)):
        stage_grids.append({
            "label": label,
            "png": _grid_png(np.asarray(grid, dtype=np.float64)),
            "meta": _grid_meta(np.asarray(grid, dtype=np.float64)),
        })

    assets = result.assets_payload(min_exposure=0.0)
    assets.sort(key=lambda a: -a["priority"])
    sim_id = f"{req.hazard_type}-{uuid.uuid4().hex[:8]}"

    # keep the heavy grids out of the saved JSON; assessments + summary fit
    try:
        save_simulation(sim_id, {
            "sim_id": sim_id, "hazard_type": req.hazard_type,
            "scenario": result.scenario, "summary": result.summary,
            "stage_labels": result.stage_labels,
        })
    except Exception:  # saving must never break a live simulation
        logger.exception("failed to save simulation %s", sim_id)

    a, b, c, d, e, f = terrain.transform
    return {
        "sim_id": sim_id,
        "hazard_type": req.hazard_type,
        "params": params,
        "elapsed_s": elapsed,
        "grid": {
            "transform": [a, b, c, d, e, f],
            "height": terrain.height,
            "width": terrain.width,
        },
        "stages": stage_grids,
        "stage_labels": result.stage_labels,
        "summary": result.summary,
        "assets": assets,
        "cascade_edges": _cascade_edges(state["dep_graph"], assets),
    }


@app.get("/api/simulations")
def simulations() -> list[dict]:
    return list_simulations()


@app.get("/api/simulations/{sim_id}")
def simulation_detail(sim_id: str) -> dict:
    payload = load_simulation(sim_id)
    if payload is None:
        raise HTTPException(404, f"simulation {sim_id!r} not found")
    return payload


@app.get("/api/dependencies/{asset_id:path}")
def dependencies(asset_id: str) -> dict:
    """Direct upstream dependencies + downstream dependents of one asset."""
    state = _get_state()
    g = state["dep_graph"]
    if asset_id not in g:
        raise HTTPException(404, f"asset {asset_id!r} not in dependency graph")
    layers_by_id = {}
    for gdf in state["layers"].values():
        if gdf.empty or "osm_type" not in gdf.columns:
            continue
        for _, row in gdf.iterrows():
            layers_by_id[f"{row['osm_type']}/{row['osm_id']}"] = row.get("name")

    def node(nid: str) -> dict:
        d = g.nodes[nid]
        return {"id": nid, "group": d.get("group", ""),
                "name": d.get("name") or layers_by_id.get(nid)}

    return {
        "asset": node(asset_id),
        "depends_on": [
            {**node(v), "dep_type": d.get("dep_type", ""),
             "weight": round(float(d.get("weight", 1.0)), 3)}
            for _, v, d in g.out_edges(asset_id, data=True)
        ],
        "dependents": [
            {**node(u), "dep_type": d.get("dep_type", ""),
             "weight": round(float(d.get("weight", 1.0)), 3)}
            for u, _, d in g.in_edges(asset_id, data=True)
        ],
    }


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Dashboard static files (imported Leaflet, no CDN dependency). Mounted last
# so /api/* routes take precedence.
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
if _WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
