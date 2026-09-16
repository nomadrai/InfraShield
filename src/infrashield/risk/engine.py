"""Exposure sampling and asset risk computation.

Exposure: hazard intensity sampled from the simulated grids at each asset's
actual geography (point assets: the containing cell; line/polygon assets:
max over up to 24 densified sample points, so a long bridge cannot hide
from a flooded segment). Flood: water depth at the asset; heat: heat index;
cyclone: wind fraction and surge depth.

Sampling is fully vectorised: every geometry contributes its sample points
to flat ``lons/lats`` arrays once (``build_geometry_samples``); each hazard
grid is then sampled for all points in a single fancy-indexed lookup and
reduced per asset with ``np.maximum.at`` / ``np.add.at``. No per-geometry
Python loops per stage, no attribute caching on shapely objects (whose
``__getattr__`` delegation is very slow).

Asset risk follows the brief: risk = exposure x vulnerability x criticality
(normalised 0..1, components stored for explainability). The result of a
simulation is one ``AssetAssessment`` per asset with everything the UI
needs: per-stage exposure, final risk components, and later the network/
cascade impact scores attached by the graph stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from infrashield.hazard.terrain import TerrainData
from infrashield.risk.criticality import asset_params, elevation_vulnerability

MAX_SAMPLE_POINTS = 24  # densified samples per non-point geometry


def _depth_exposure(depth_m: float) -> float:
    """Water depth -> exposure 0..1 (serviceability loss for ground floors)."""
    if depth_m <= 0.02:
        return 0.0
    # 0.3 m: knee height / ground-floor damage begins; 1.5 m+: uninhabitable
    return float(min(1.0, (depth_m / 1.5) ** 0.85))


def _wind_exposure(wind_frac: float) -> float:
    return float(min(1.0, max(0.0, wind_frac)))


def _heat_exposure(heat_index: float) -> float:
    return float(min(1.0, max(0.0, heat_index)))


def geom_sample_points(geom, max_pts: int = MAX_SAMPLE_POINTS) -> list[tuple[float, float]]:
    """Representative sample points of any geometry (densified lines)."""
    gtype = geom.geom_type
    if gtype == "Point":
        return [(geom.x, geom.y)]
    if gtype in ("LineString", "LinearRing"):
        coords = list(geom.coords)
    elif gtype == "MultiLineString":
        coords = [pt for part in geom.geoms for pt in part.coords]
    elif gtype == "Polygon":
        coords = list(geom.exterior.coords)
    elif gtype == "MultiPolygon":
        coords = [pt for p in geom.geoms for pt in p.exterior.coords]
    else:
        c = geom.centroid
        return [(c.x, c.y)]
    if len(coords) > max_pts:
        idx = np.linspace(0, len(coords) - 1, max_pts).astype(int)
        coords = [coords[i] for i in idx]
    return [(float(lon), float(lat)) for lon, lat in coords]


def build_geometry_samples(geoms, terrain: TerrainData) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Flatten all geometries' sample points once.

    Returns ``(owner, lons, lats, n_geoms)`` where ``owner[i]`` is the index
    of the geometry that sample ``i`` belongs to.
    """
    owner: list[int] = []
    lons: list[float] = []
    lats: list[float] = []
    for i, geom in enumerate(geoms):
        if geom is None or geom.is_empty:
            continue
        for lon, lat in geom_sample_points(geom):
            owner.append(i)
            lons.append(lon)
            lats.append(lat)
    n = len(geoms)
    if not owner:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), n
    return (np.asarray(owner, dtype=np.int64),
            np.asarray(lons, dtype=np.float64),
            np.asarray(lats, dtype=np.float64),
            n)


def sample_grid_vectorized(
    grid: np.ndarray | None,
    terrain: TerrainData,
    owner: np.ndarray,
    lons: np.ndarray,
    lats: np.ndarray,
    n_geoms: int,
    reducer: str = "max",
) -> np.ndarray:
    """Sample one hazard grid for all geometries at once (vectorised)."""
    out = np.zeros(n_geoms, dtype=np.float64)
    if grid is None or owner.size == 0:
        return out
    a, b, c, d, e, f = terrain.transform
    cols = ((lons - c) / a).astype(np.int64)
    rows = ((lats - f) / e).astype(np.int64)
    ok = (rows >= 0) & (rows < terrain.height) & (cols >= 0) & (cols < terrain.width)
    if not ok.any():
        return out
    vals = grid[rows[ok], cols[ok]]
    vals = np.where(np.isfinite(vals), vals, 0.0)
    own = owner[ok]
    if reducer == "mean":
        sums = np.zeros(n_geoms, dtype=np.float64)
        np.add.at(sums, own, vals)
        counts = np.bincount(own, minlength=n_geoms).astype(np.float64)
        counts[counts == 0] = 1.0
        out = sums / counts
    else:
        np.maximum.at(out, own, vals)
    return out


@dataclass
class AssetAssessment:
    """Full per-asset result of one simulation."""

    asset_id: str                 # "node/123" | "way/456"
    name: str | None
    group: str
    category: str
    lon: float
    lat: float
    length_km: float | None
    criticality: float
    vulnerability: float
    exposure: float               # final stage exposure 0..1
    depth_m: float | None         # flood/surge depth at asset (if applicable)
    wind_ms: float | None
    temp_c: float | None
    risk: float                   # exposure x vulnerability x criticality
    stage_exposure: list[float] = field(default_factory=list)
    impact: float = 0.0           # network + cascade impact (filled later)
    priority: float = 0.0         # combined ranking score (filled later)
    impact_depth: int = 0         # cascade depth (filled later)
    dependents: int = 0
    network_impact: float = 0.0
    cascade_impact: float = 0.0
    affected_population: int = 0
    # explainable H x V x C breakdown (see risk/scoring.py; filled by the
    # scenario orchestrator after network/cascade stages)
    risk10: float = 0.0
    band: str = "safe"
    status: str = "safe"
    hvc: dict = field(default_factory=dict)
    action: str = ""

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id, "name": self.name, "group": self.group,
            "category": self.category, "lon": self.lon, "lat": self.lat,
            "length_km": self.length_km, "criticality": self.criticality,
            "vulnerability": self.vulnerability, "exposure": round(self.exposure, 3),
            "depth_m": None if self.depth_m is None else round(self.depth_m, 2),
            "wind_ms": None if self.wind_ms is None else round(self.wind_ms, 1),
            "temp_c": None if self.temp_c is None else round(self.temp_c, 1),
            "risk": round(self.risk, 4), "impact": round(self.impact, 4),
            "priority": round(self.priority, 4),
            "impact_depth": self.impact_depth, "dependents": self.dependents,
            "network_impact": round(self.network_impact, 4),
            "cascade_impact": round(self.cascade_impact, 4),
            "stage_exposure": [round(x, 3) for x in self.stage_exposure],
            "affected_population": self.affected_population,
            "risk10": self.risk10,
            "band": self.band,
            "status": self.status,
            "hvc": self.hvc,
            "action": self.action,
        }


class ExposureSampler:
    """Scalar grid sampling at a single geometry (small-scale/manual use).

    Bulk per-stage sampling goes through :func:`sample_grid_vectorized`.
    """

    def __init__(self, terrain: TerrainData) -> None:
        self.t = terrain

    def _rowcol(self, lon: float, lat: float) -> tuple[int, int] | None:
        a, b, c, d, e, f = self.t.transform
        col = int((lon - c) / a)
        row = int((lat - f) / e)
        if 0 <= row < self.t.height and 0 <= col < self.t.width:
            return row, col
        return None

    def sample_max(self, grid: np.ndarray, geom) -> float:
        vals = []
        for lon, lat in geom_sample_points(geom):
            rc = self._rowcol(lon, lat)
            if rc is None:
                continue
            v = grid[rc[0], rc[1]]
            if np.isfinite(v):
                vals.append(float(v))
        return max(vals) if vals else 0.0

    def sample_mean(self, grid: np.ndarray, geom) -> float:
        vals = []
        for lon, lat in geom_sample_points(geom):
            rc = self._rowcol(lon, lat)
            if rc is None:
                continue
            v = grid[rc[0], rc[1]]
            if np.isfinite(v):
                vals.append(float(v))
        return float(np.mean(vals)) if vals else 0.0


def assess_assets(
    assets_gdf,                    # GeoDataFrame in WGS84 with group_name/category/tags
    terrain: TerrainData,
    hazard_fields: dict,           # {"depth": grid} | {"heat_index": grid} | {"wind_frac": grid, "surge_depth": grid}
    hazard_type: str,              # flood | heat | cyclone
) -> list[AssetAssessment]:
    """Sample hazard fields at every asset and compute asset risk.

    Points sample their containing cell; lines/polygons sample up to 24
    densified vertices and keep the max (mean for heat, where a local
    average is the physical reality of an air-mass hazard).
    """
    dem = np.where(np.isfinite(terrain.dem), terrain.dem, np.nan)
    elev_min = float(np.nanmin(dem))
    elev_max = float(np.nanmax(dem))
    depth_grid = hazard_fields.get("depth")
    wind_grid = hazard_fields.get("wind_frac")
    surge_grid = hazard_fields.get("surge_depth")
    heat_grid = hazard_fields.get("heat_index")
    susceptibility_grid = hazard_fields.get("susceptibility")

    geoms = list(assets_gdf.geometry)
    owner, lons, lats, n = build_geometry_samples(geoms, terrain)

    def sample(grid, reducer: str = "max") -> np.ndarray:
        return sample_grid_vectorized(grid, terrain, owner, lons, lats, n, reducer)

    depth_vals = sample(depth_grid) if hazard_type == "flood" else None
    heat_vals = sample(heat_grid, reducer="mean") if hazard_type == "heat" else None
    wind_vals = sample(wind_grid) if hazard_type == "cyclone" else None
    surge_vals = sample(surge_grid) if hazard_type == "cyclone" else None
    sus_vals = (sample(susceptibility_grid, reducer="mean")
                if hazard_type == "landslide" else None)

    out: list[AssetAssessment] = []
    for idx, (_, row) in enumerate(assets_gdf.iterrows()):
        params = asset_params(row)
        crit = params["criticality"]
        vuln = params["vulnerability"]

        # representative point: geometry centroid (points: themselves)
        geom = geoms[idx]
        rep = geom if geom.geom_type == "Point" else geom.centroid
        rep_x, rep_y = float(rep.x), float(rep.y)
        a, b, c, d, e, f = terrain.transform
        col = int((rep_x - c) / a)
        rowi = int((rep_y - f) / e)
        ground = None
        if 0 <= rowi < terrain.height and 0 <= col < terrain.width:
            g = dem[rowi, col]
            if np.isfinite(g):
                ground = float(g)
        if ground is not None:
            vuln = elevation_vulnerability(vuln, ground, elev_min, elev_max)

        depth_m = None
        wind_ms = None
        temp_c = None
        if hazard_type == "flood":
            depth_m = float(depth_vals[idx])
            exposure = _depth_exposure(depth_m)
            stage_exp: list[float] = []
        elif hazard_type == "heat":
            hi = float(heat_vals[idx])
            exposure = _heat_exposure(hi)
            # buildings/roads suffer less acute heat damage than people do
            if row.get("group_name") in ("roads", "transport"):
                exposure *= 0.35
            temp_c = 33.0 + hi * 12.0
            stage_exp = []
        elif hazard_type == "cyclone":
            e_wind = _wind_exposure(float(wind_vals[idx]))
            e_surge = _depth_exposure(float(surge_vals[idx]))
            exposure = max(e_wind, e_surge)
            # Roads fail from wind only via flying debris / surface flooding;
            # a sealed carriageway survives gales that unroof buildings.
            if row.get("group_name") in ("roads", "transport"):
                e_wind *= 0.4
            exposure = max(e_wind, e_surge)
            sd = float(surge_vals[idx])
            depth_m = sd if sd > 0 else None
            wind_ms = float(wind_vals[idx]) * 60.0
            stage_exp = []
        elif hazard_type == "landslide":
            sus = float(sus_vals[idx])
            exposure = float(min(1.0, max(0.0, sus)))
            # Buried/impacted by moving debris: roads on steep ground are the
            # classic first loss; facilities fail on access, not burial.
            if row.get("group_name") in ("health_education", "energy", "communication", "water"):
                exposure *= 0.6  # facilities fail on access/backup, not burial
            stage_exp = []
        else:
            exposure = 0.0
            stage_exp = []

        # risk = exposure x vulnerability x criticality, all 0..1
        risk = float(min(1.0, exposure * vuln * crit))

        tags_raw = row.get("tags")
        name = row.get("name")
        if (name is None or name == "") and isinstance(tags_raw, str):
            try:
                name = json.loads(tags_raw).get("name")
            except json.JSONDecodeError:
                name = None
        out.append(AssetAssessment(
            asset_id=f"{row['osm_type']}/{row['osm_id']}",
            name=name,
            group=str(row.get("group_name", "")),
            category=str(row.get("category", "")),
            lon=rep_x,
            lat=rep_y,
            length_km=float(row["length_km"]) if row.get("length_km") is not None else None,
            criticality=crit,
            vulnerability=round(vuln, 3),
            exposure=exposure,
            depth_m=depth_m,
            wind_ms=wind_ms,
            temp_c=temp_c,
            risk=risk,
            stage_exposure=stage_exp,
        ))
    return out
