"""Persistence: layers, dependency graph, and simulation results.

The primary store is the file system (``data/processed/`` + ``data/sim/``)
so the dashboard works without a database. When PostGIS is reachable
(``docker compose up``), the same data lives in ``infra_asset`` /
``infra_dependency`` / ``asset_risk_score`` per sql/001_schema.sql; the
loaders here mirror that schema's semantics (asset ids ``type/id``,
dep edges with weight + dep_type) so a later DB swap is mechanical.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd

from infrashield.config import DATA_DIR, PROCESSED_DATA_DIR, load_categories

logger = logging.getLogger(__name__)

SIM_DIR = DATA_DIR / "sim"

# Terrain preprocessing (fill -> D8 -> accumulation -> depressions) takes
# ~20-40 s on the 10x10 km AOI; the result is a pure function of the DEM so
# cache it in-process. TerrainData holds large numpy arrays, so we never
# pickle it to disk.
_terrain_cache: dict[str, "TerrainData"] = {}


def load_terrain():
    """Load + preprocess the study-area DEM (cached in-process)."""
    from infrashield.hazard.terrain import preprocess

    dem_path = DATA_DIR / "raw" / "dem" / "dem_mumbai.tif"
    if not dem_path.exists():
        raise FileNotFoundError(
            f"DEM not found at {dem_path}; run `make fetch-dem` or "
            "python scripts/fetch_dem.py first"
        )
    key = str(dem_path)
    if key not in _terrain_cache:
        logger.info("Preprocessing DEM %s ...", dem_path)
        _terrain_cache[key] = preprocess(dem_path)
        logger.info("Terrain ready: %dx%d cells, %d depressions",
                    _terrain_cache[key].height, _terrain_cache[key].width,
                    len(_terrain_cache[key].depressions))
    return _terrain_cache[key]


def _layer_group_map() -> dict[str, tuple[str, str]]:
    """layer name -> (category, group) from the category registry."""
    try:
        return {c.layer: (c.name, c.group) for c in load_categories()}
    except FileNotFoundError:
        return {}


def load_layers(layers: list[str] | None = None) -> dict[str, gpd.GeoDataFrame]:
    """Load processed GeoJSON layers into memory (WGS84)."""
    geo_dir = PROCESSED_DATA_DIR / "geojson"
    out: dict[str, gpd.GeoDataFrame] = {}
    if not geo_dir.exists():
        return out
    for path in sorted(geo_dir.glob("*.geojson")):
        name = path.stem
        if layers and name not in layers:
            continue
        try:
            gdf = gpd.read_file(path)
            if not gdf.empty:
                cat, group = _layer_group_map().get(name, (name, "other"))
                gdf["category"] = cat
                gdf["group_name"] = group
                out[name] = gdf
        except Exception:
            logger.exception("Failed to read layer %s", path)
    return out


def build_dependency_graph(layers: dict[str, gpd.GeoDataFrame]):
    """Build the proximity dependency graph over all asset layers."""
    from infrashield.graph.dependencies import GraphBuildConfig, build_proximity_graph

    import pandas as pd

    frames = [gdf for gdf in layers.values() if not gdf.empty]
    if not frames:
        return nx.DiGraph()
    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True),
                                crs=frames[0].crs)
    return build_proximity_graph(combined, GraphBuildConfig())


def graph_to_payload(graph: nx.DiGraph) -> dict:
    nodes = [
        {"id": n, "group": d.get("group", ""), "name": d.get("name")}
        for n, d in graph.nodes(data=True)
    ]
    edges = [
        {"source": u, "target": v, "dep_type": d.get("dep_type", ""),
         "weight": d.get("weight", 1.0), "distance_m": d.get("distance_m")}
        for u, v, d in graph.edges(data=True)
    ]
    return {"nodes": nodes, "edges": edges}


def save_simulation(sim_id: str, payload: dict) -> Path:
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    path = SIM_DIR / f"{sim_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    logger.info("Saved simulation %s (%.1f KB)", sim_id, path.stat().st_size / 1024)
    return path


def load_simulation(sim_id: str) -> dict | None:
    path = SIM_DIR / f"{sim_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_simulations() -> list[dict]:
    if not SIM_DIR.exists():
        return []
    out = []
    for path in sorted(SIM_DIR.glob("*.json")):
        try:
            head = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "sim_id": head.get("sim_id", path.stem),
                "hazard_type": head.get("hazard_type"),
                "scenario": head.get("scenario"),
                "summary": head.get("summary"),
                "created": path.stat().st_mtime,
            })
        except json.JSONDecodeError:
            continue
    return out
