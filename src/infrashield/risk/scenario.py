"""Scenario orchestration: hazard -> exposure -> risk -> network -> cascade.

One ``run_scenario`` call produces the complete ``SimulationResult`` the
API/UI consumes:

1. Hazard generation on the real terrain (flood / heat / cyclone models).
2. Exposure sampling at each asset's actual geography.
3. Asset risk = exposure x vulnerability x criticality.
4. Network impact: road graph degradation, lost connectivity, detours,
   accessibility increases to critical facilities.
5. Cascade impact through the infrastructure dependency graph.
6. Priority score, deliberately separating **risk** (likelihood the asset
   fails) from **impact** (consequence if it fails):

   impact = w1*criticality + w2*network_impact + w3*cascade_impact
            + w4*population + w5*redundancy_loss,   then
   priority = max(risk, impact) + 0.3 * min(risk, impact)

   so a highly critical but barely exposed asset still ranks on impact,
   while a badly exposed minor road ranks on risk alone.
"""

from __future__ import annotations

import ast
import json
import logging
import math
from dataclasses import dataclass, field

import numpy as np

from infrashield.hazard.cyclone import CycloneScenario, CycloneSimulator
from infrashield.hazard.flood import FloodScenario, FloodSimulator
from infrashield.hazard.heat import HeatScenario, HeatSimulator
from infrashield.hazard.terrain import TerrainData
from infrashield.risk.cascade import compute_cascade_impacts
from infrashield.risk.engine import AssetAssessment, assess_assets
from infrashield.risk.network import RoadNetwork

logger = logging.getLogger(__name__)

# Priority weights (documented in docs/algorithm.md)
W_CRIT, W_NET, W_CASC, W_POP, W_RED = 0.30, 0.30, 0.20, 0.15, 0.05
POP_PER_CAPITA_FACTOR = 2200  # residents served per critical facility, order-of-magnitude


@dataclass
class SimulationResult:
    hazard_type: str
    scenario: dict
    stages: list[np.ndarray]
    stage_labels: list[str]
    stage_grids_meta: dict            # transform, crs, shape for frontend raster
    assessments: list[AssetAssessment]
    summary: dict = field(default_factory=dict)
    network_stats: dict = field(default_factory=dict)

    def assets_payload(self, min_exposure: float = 0.01) -> list[dict]:
        return [
            a.to_dict() for a in self.assessments
            if a.exposure >= min_exposure or a.risk > 0 or a.impact > 0
        ]


def _roads_for_network(all_layers: dict) -> "object | None":
    """Concatenate road-ish layers for network building."""
    import geopandas as gpd

    frames = []
    for key in ("roads_major", "roads_minor", "bridges"):
        gdf = all_layers.get(key)
        if gdf is not None and not gdf.empty:
            frames.append(gdf)
    if not frames:
        return None
    return gpd.GeoDataFrame(
        __import__("pandas").concat(frames, ignore_index=True), crs=frames[0].crs
    )


def _landcover_masks(all_layers: dict, terrain: TerrainData) -> dict[str, np.ndarray | None]:
    """Rasterise OSM land-cover to boolean grids aligned with the DEM.

    * ``builtup``: residential/industrial/commercial/retail landuse polygons
      (urban heat island: heat storage + anthropogenic waste heat)
    * ``water``: natural=water polygons and waterway lines (evaporative
      cooling)
    ``green`` would come from leisure=park etc., which the current extract
    does not include, so it stays None and simply contributes nothing.
    """
    import json as _json

    import numpy as np
    import pandas as pd
    from rasterio.features import rasterize

    def _tagged(gdf, keys: set[str]):
        out = []
        if gdf is None or gdf.empty:
            return out
        for raw in gdf.get("tags", pd.Series(dtype=object)):
            try:
                tags = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            except _json.JSONDecodeError:
                continue
            for k in keys:
                if tags.get(k):
                    out.append(tags[k])
        return out

    masks: dict[str, np.ndarray | None] = {"builtup": None, "water": None, "green": None}
    shape = (terrain.height, terrain.width)
    res = all_layers.get("residential")
    ww = all_layers.get("waterways")

    if res is not None and not res.empty:
        built_vals = set(_tagged(res, {"landuse"}))
        built_types = built_vals & {"residential", "industrial", "commercial", "retail"}
        if built_types:
            sel = res[res["tags"].str.contains(
                "|".join(sorted(f'"landuse": "{t}"' for t in built_types)), na=False
            )]
            if not sel.empty:
                masks["builtup"] = rasterize(
                    ((g, 1) for g in sel.geometry if g is not None and not g.is_empty),
                    out_shape=shape, transform=terrain.transform,
                    fill=0, all_touched=True, dtype="uint8",
                ).astype(bool)
        water_types = {"water"} & set(_tagged(res, {"natural"}))
        if water_types:
            sel = res[res["tags"].str.contains('"natural": "water"', na=False)]
            if not sel.empty:
                masks["water"] = rasterize(
                    ((g, 1) for g in sel.geometry if g is not None and not g.is_empty),
                    out_shape=shape, transform=terrain.transform,
                    fill=0, all_touched=True, dtype="uint8",
                ).astype(bool)
    if masks["water"] is None and ww is not None and not ww.empty:
        masks["water"] = rasterize(
            ((g, 1) for g in ww.geometry if g is not None and not g.is_empty),
            out_shape=shape, transform=terrain.transform,
            fill=0, all_touched=True, dtype="uint8",
        ).astype(bool)
    return masks


def run_scenario(
    terrain: TerrainData,
    all_layers: dict,                # layer name -> GeoDataFrame (WGS84)
    dep_graph,                       # networkx DiGraph of dependencies
    hazard_type: str,
    params: dict,
) -> SimulationResult:
    """Execute the full risk + impact pipeline for one scenario."""
    assets = []
    for layer, gdf in all_layers.items():
        if layer in ("waterways", "residential"):
            continue
        if gdf is None or gdf.empty:
            continue
        assets.append(gdf)
    import geopandas as gpd
    import pandas as pd

    assets_gdf = gpd.GeoDataFrame(
        pd.concat(assets, ignore_index=True), crs=assets[0].crs if assets else None
    )
    # The same OSM way can appear in several layers (a road that is also a
    # bridge); keep the first occurrence so every asset is assessed once.
    before = len(assets_gdf)
    assets_gdf = assets_gdf.drop_duplicates(subset=["osm_type", "osm_id"], keep="first")
    logger.info(
        "Scenario %s: %d assets (%d dups removed) from %d layers",
        hazard_type, len(assets_gdf), before - len(assets_gdf), len(assets),
    )

    # ---------------- 1-2: hazard fields ---------------- #
    hazard_meta: dict = {}
    stages: list[np.ndarray] = []
    stage_labels: list[str] = []
    fields: dict[str, np.ndarray] = {}
    transform = terrain.transform
    stage_meta = {"transform": list(transform), "crs": terrain.crs,
                  "height": terrain.height, "width": terrain.width}

    if hazard_type == "flood":
        sim = FloodSimulator(terrain)
        scenario = FloodScenario(
            rainfall_mm=float(params.get("rainfall_mm", 250)),
            duration_h=float(params.get("duration_h", 24)),
            sea_level_m=float(params.get("sea_level_m", 0.0)),
            n_stages=int(params.get("n_stages", 8)),
            name=str(params.get("name", "flood")),
        )
        result = sim.run(scenario)
        stages = result.stages
        stage_labels = result.stage_labels
        fields = {"depth": result.depths}
        hazard_meta = {
            "wet_fraction": result.wet_fraction,
            "max_depth_m": result.max_depth,
            "flood_volume_m3": result.flood_volume_m3,
            "n_depressions": result.meta.get("n_depressions"),
            "channel_overflow_max_m": result.meta.get("channel_overflow_max_m"),
        }
    elif hazard_type == "heat":
        masks = _landcover_masks(all_layers, terrain)
        sim = HeatSimulator(
            terrain,
            green_cells=masks["green"],
            water_cells=masks["water"],
            builtup_cells=masks["builtup"],
        )
        scenario = HeatScenario(
            anomaly_c=float(params.get("anomaly_c", 8.0)),
            normal_max_c=float(params.get("normal_max_c", 33.0)),
            name=str(params.get("name", "heatwave")),
        )
        result = sim.run(scenario)
        stages = [result["heat_index"]]
        stage_labels = ["peak afternoon"]
        fields = {"heat_index": result["heat_index"]}
        hazard_meta = {"max_temp_c": result["max_temp"], "mean_temp_c": result["mean_temp"]}
    elif hazard_type == "cyclone":
        sim = CycloneSimulator(terrain)
        scenario = CycloneScenario(
            category=int(params.get("category", 3)),
            landfall_lon=float(params.get("landfall_lon", 72.78)),
            landfall_lat=float(params.get("landfall_lat", 19.13)),
            name=str(params.get("name", "cyclone")),
        )
        result = sim.run(scenario)
        stages = [result["surge_depth_m"], result["wind_frac"]]
        stage_labels = ["storm surge", "peak wind"]
        fields = {"wind_frac": result["wind_frac"], "surge_depth": result["surge_depth_m"]}
        hazard_meta = {"peak_wind_ms": result["peak_wind_ms"], "max_surge_m": result["max_surge_m"]}
    else:
        raise ValueError(f"Unknown hazard type: {hazard_type}")

    # ---------------- 3: exposure + asset risk ---------------- #
    assessments = assess_assets(assets_gdf, terrain, fields, hazard_type)
    if hazard_type == "flood":
        # per-stage exposure from the actual stage grids (this is what the
        # frontend animation replays) - vectorised over all assets x stages,
        # max over each geometry's sample points (same rule as final exposure)
        from infrashield.risk.engine import (
            build_geometry_samples,
            sample_grid_vectorized,
        )

        geoms = list(assets_gdf.geometry)
        owner, lons, lats, n = build_geometry_samples(geoms, terrain)
        # assessments were created in assets_gdf row order, so geometry
        # index i corresponds to assessments[i]
        for stage_grid in stages:
            depths = sample_grid_vectorized(stage_grid, terrain, owner, lons, lats, n)
            for a2, depth in zip(assessments, depths):
                a2.stage_exposure.append(
                    round(min(1.0, (float(depth) / 1.5) ** 0.85), 3)
                    if depth > 0.02 else 0.0
                )
    risk_by_asset = {a.asset_id: a.risk for a in assessments}
    # Passability loss for road edges: use exposure (physical disruption)
    # rather than consequence-weighted risk — a flooded lane is impassable
    # regardless of how critical the street is.
    usability = {
        a.asset_id: max(a.risk, a.exposure * 0.9) for a in assessments
    }

    # ---------------- 4: network impact ---------------- #
    network_stats: dict = {}
    roads = _roads_for_network(all_layers)
    if roads is not None and not roads.empty:
        base_net = RoadNetwork(roads)
        deg_net = base_net.degraded_copy(usability)
        lost = deg_net.lost_connectivity(base_net)
        detours = deg_net.detour_minutes(base_net)
        # attribute detour impact to the failed assets via a lookup (no O(n^2))
        asset_by_id = {a.asset_id: a for a in assessments}
        for edge_key, extra in detours.items():
            u_s, v_s = edge_key.split("|")
            key = (ast.literal_eval(u_s), ast.literal_eval(v_s))
            for asset_id in base_net.edge_asset.get(key, []):
                a = asset_by_id.get(asset_id)
                if a is not None:
                    a.network_impact = max(
                        a.network_impact, min(1.0, (extra or 45.0) / 45.0)
                    )
        # accessibility: demand = residential-area centroids, facilities = hospitals/emergency
        fac_mask = assets_gdf.get("category", pd.Series("", index=assets_gdf.index)).isin(
            ["hospitals"]
        )
        fac = assets_gdf[fac_mask]
        res = all_layers.get("residential")
        if res is not None and not res.empty and not fac.empty:
            fac_pts = [(g.centroid.x, g.centroid.y) for g in fac.geometry]
            dem_pts = [(g.centroid.x, g.centroid.y) for g in res.geometry.head(150)]
            extra_min, isolated_frac = deg_net.accessibility_impact(base_net, fac_pts, dem_pts)
            network_stats["accessibility_increase_min"] = round(extra_min, 1)
            network_stats["isolated_demand_fraction"] = round(isolated_frac, 3)
        network_stats.update({
            "road_edges": base_net.graph.number_of_edges(),
            "road_nodes": base_net.graph.number_of_nodes(),
            "failed_edges": len(detours),
            "lost_connectivity_fraction": round(lost, 4),
            "max_detour_min": round(max((v or 45.0) for v in detours.values()), 1) if detours else 0.0,
        })

    # ---------------- 5: cascade impact ---------------- #
    # Failure = the same exposure-driven definition the road network uses,
    # so "flooded out" assets propagate consequences downstream.
    cascades = compute_cascade_impacts(dep_graph, usability)
    for a in assessments:
        info = cascades.get(a.asset_id)
        if info:
            a.cascade_impact = info["cascade"]
            a.impact_depth = info["depth"]
            a.dependents = info["dependents"]

    # ---------------- 6: priority scoring ---------------- #
    # Population model: service-population is attributed to *facility* assets
    # (hospitals, schools, utilities, transport hubs) -- road segments do not
    # "serve" an independent population or every person would be counted
    # once per street. W_POP normalises by POP_PER_CAPITA_FACTOR so the pop
    # term is bounded like the others.
    FACILITY_GROUPS = {"health_education", "energy", "communication", "transport", "water"}
    for a in assessments:
        materially_affected = a.exposure > 0.05 or a.network_impact > 0 or a.cascade_impact > 0
        pop = (
            int(round(a.criticality * POP_PER_CAPITA_FACTOR * (0.3 + 0.7 * a.exposure)))
            if materially_affected and a.group in FACILITY_GROUPS else 0
        )
        a.affected_population = pop
        redundancy_loss = a.network_impact
        impact = (
            W_CRIT * a.criticality
            + W_NET * a.network_impact
            + W_CASC * a.cascade_impact
            + W_POP * min(1.0, pop / POP_PER_CAPITA_FACTOR)
            + W_RED * redundancy_loss
        )
        a.impact = float(min(1.35, impact))
        a.priority = float(max(a.risk, a.impact) + 0.3 * min(a.risk, a.impact))

    summary = {
        "hazard": hazard_meta,
        "assets_total": len(assessments),
        "assets_exposed": sum(1 for a in assessments if a.exposure > 0.05),
        "assets_high_risk": sum(1 for a in assessments if a.risk >= 0.45),
        "assets_high_impact": sum(1 for a in assessments if a.impact >= 0.5),
        "affected_population": int(sum(a.affected_population for a in assessments)),
        "by_group": {
            g: {
                "exposed": sum(1 for a in assessments if a.group == g and a.exposure > 0.05),
                "high_risk": sum(1 for a in assessments if a.group == g and a.risk >= 0.45),
            }
            for g in sorted({a.group for a in assessments})
        },
        "network": network_stats,
    }
    logger.info("Scenario done: %s", json.dumps({k: v for k, v in summary.items() if k != 'by_group'}, default=str)[:400])

    return SimulationResult(
        hazard_type=hazard_type,
        scenario={"type": hazard_type, **params},
        stages=stages,
        stage_labels=stage_labels,
        stage_grids_meta=stage_meta,
        assessments=assessments,
        summary=summary,
        network_stats=network_stats,
    )


def _geom_of(a: AssetAssessment):
    """Rebuild a point geometry for stage sampling (kept tiny)."""
    from shapely.geometry import Point

    return Point(a.lon, a.lat)
