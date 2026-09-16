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
import numpy as np
import pandas as pd

from infrashield.config import DATA_DIR, PROCESSED_DATA_DIR, load_categories

logger = logging.getLogger(__name__)

SIM_DIR = DATA_DIR / "sim"

# Terrain preprocessing (fill -> D8 -> accumulation -> depressions) takes
# ~20-40 s on the 10x10 km AOI; the result is a pure function of the DEM so
# cache it in-process AND on disk (data/processed/terrain_cache.npz).
#
# Why a disk cache: TerrainData cannot be pickled wholesale (it embeds
# callables/objects via the depression dicts), so the process cache used to
# be the only layer - every restart paid the full 20-40 s again. The derived
# arrays are a deterministic function of the DEM file, so we persist them
# under an md5(dem bytes) key and rebuild the object in milliseconds.
# Depressions are round-tripped separately (json + cell indices) because
# their dicts also hold z_sorted/vol_at_level arrays.
_terrain_cache: dict[str, "TerrainData"] = {}
_TERRAIN_CACHE_FILE = PROCESSED_DATA_DIR / "terrain_cache.npz"


def _terrain_cache_key(dem_path: Path) -> str:
    """Content hash of the DEM file: invalidate the cache when the raster changes."""
    import hashlib

    h = hashlib.md5()
    with dem_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _save_terrain_cache(key: str, t: "TerrainData") -> None:
    try:
        PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            _TERRAIN_CACHE_FILE,
            key=key,
            dem=t.dem, filled=t.filled, fill_depth=t.fill_depth,
            flow_dir=t.flow_dir, flow_acc=t.flow_acc, slope=t.slope,
            channel_mask=t.channel_mask,
            cell_area_m2=t.cell_area_m2,
            transform=np.array(t.transform, dtype=np.float64),
            crs=t.crs, height=t.height, width=t.width,
            n_depressions=len(t.depressions),
            dep_meta=json.dumps([
                {k: v for k, v in d.items()
                 if k not in ("cells", "z_sorted", "vol_at_level")}
                for d in t.depressions
            ]),
            dep_cells=np.concatenate([
                d["cells"] for d in t.depressions
            ]) if t.depressions else np.empty(0, dtype=np.int64),
            dep_z=np.concatenate([
                d["z_sorted"] for d in t.depressions
            ]) if t.depressions else np.empty(0),
            dep_vol=np.concatenate([
                d["vol_at_level"] for d in t.depressions
            ]) if t.depressions else np.empty(0),
            dep_lens=np.array(
                [[len(d["cells"]), len(d["z_sorted"]), len(d["vol_at_level"])]
                 for d in t.depressions], dtype=np.int64) if t.depressions
                else np.empty((0, 3), dtype=np.int64),
        )
        logger.info("Terrain cache written: %s", _TERRAIN_CACHE_FILE)
    except Exception:
        # caching is an optimisation; a failure must never break the API
        logger.exception("failed to write terrain cache %s", _TERRAIN_CACHE_FILE)


def _load_terrain_cache(key: str) -> "TerrainData | None":
    if not _TERRAIN_CACHE_FILE.exists():
        return None
    try:
        import json as _json

        with np.load(_TERRAIN_CACHE_FILE, allow_pickle=False) as z:
            if str(z["key"]) != key:
                logger.info("Terrain cache stale (DEM changed); reprocessing")
                return None
            depressions: list[dict] = []
            dep_meta = _json.loads(str(z["dep_meta"]))
            if dep_meta:
                cells, z_sorted, vol, lens = (
                    z["dep_cells"], z["dep_z"], z["dep_vol"], z["dep_lens"])
                co = zo = vo = 0
                for i, meta in enumerate(dep_meta):
                    nc, nz, nv = (int(x) for x in lens[i])
                    depressions.append({
                        **meta,
                        "id": i,
                        "cells": cells[co:co + nc].copy(),
                        "z_sorted": z_sorted[zo:zo + nz].copy(),
                        "vol_at_level": vol[vo:vo + nv].copy(),
                    })
                    co += nc; zo += nz; vo += nv
            from infrashield.hazard.terrain import TerrainData

            t = TerrainData(
                dem=z["dem"], filled=z["filled"], fill_depth=z["fill_depth"],
                flow_dir=z["flow_dir"], flow_acc=z["flow_acc"], slope=z["slope"],
                cell_area_m2=float(z["cell_area_m2"]),
                channel_mask=z["channel_mask"],
                depressions=depressions,
                transform=tuple(float(v) for v in z["transform"]),
                crs=str(z["crs"]), height=int(z["height"]), width=int(z["width"]),
            )
        logger.info("Terrain loaded from disk cache in ~0.1 s (DEM md5 %s...)", key[:8])
        return t
    except Exception:
        # corrupt or unreadable cache: fall through to a full reprocess
        logger.exception("failed to read terrain cache %s", _TERRAIN_CACHE_FILE)
        return None


def load_terrain():
    """Load + preprocess the study-area DEM (in-process + on-disk cached).

    First ever call: full preprocessing (~20-40 s), result saved to
    data/processed/terrain_cache.npz. Later calls (any process) load the
    cached arrays in well under a second.
    """
    from infrashield.hazard.terrain import preprocess

    dem_path = DATA_DIR / "raw" / "dem" / "dem_mumbai.tif"
    if not dem_path.exists():
        raise FileNotFoundError(
            f"DEM not found at {dem_path}; run `make fetch-dem` or "
            "python scripts/fetch_dem.py first"
        )
    key = _terrain_cache_key(dem_path)
    if key not in _terrain_cache:
        cached = _load_terrain_cache(key)
        if cached is not None:
            _terrain_cache[key] = cached
        else:
            logger.info("Preprocessing DEM %s (no disk cache) ...", dem_path)
            _terrain_cache[key] = preprocess(dem_path)
            _save_terrain_cache(key, _terrain_cache[key])
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
