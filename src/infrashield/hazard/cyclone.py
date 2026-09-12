"""Cyclone hazard: wind field + wind-driven storm surge on the real terrain.

Simplified parametric model (assumptions documented in docs/algorithm.md):

* **Wind**: a parametric gradient wind around the storm centre
  (Holland-like shape without the fine structure): winds ramp from calm at
  the storm edge to a peak near the radius of maximum wind, decaying
  inward. Land friction reduces winds past the coastline.
* **Surge**: onshore wind stress piles water onto the coast; the surge
  height scales with peak wind squared and decays with distance inland
  (no full hydrodynamic solve). The surge level is then flooded over the
  DEM exactly like the coastal offset in the flood model.

Exposure metric: cell wind speed (m/s) + inundation from surge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from infrashield.hazard.terrain import TerrainData


@dataclass
class CycloneScenario:
    category: int                 # 1..5 (Saffir-Simpson)
    landfall_lon: float
    landfall_lat: float
    name: str = "cyclone"

    @property
    def peak_wind_ms(self) -> float:
        # Saffir-Simpson 1-min sustained winds (m/s), lower bound per category.
        bounds = {1: 33.0, 2: 43.0, 3: 50.0, 4: 58.0, 5: 70.0}
        return bounds.get(int(self.category), 33.0)

    @property
    def radius_max_wind_km(self) -> float:
        return max(12.0, 30.0 - 2.0 * self.category)

    @property
    def storm_radius_km(self) -> float:
        return 90.0 + 20.0 * self.category


class CycloneSimulator:
    """Static wind + surge field for one landfall configuration."""

    def __init__(self, terrain: TerrainData, coastline_km: float = 8.0) -> None:
        self.t = terrain
        # Approximate distance from the AOI's east edge to the Arabian Sea;
        # Mumbai's coastline in this AOI runs along the west (in km).
        self.coastline_km = coastline_km

    def run(self, scenario: CycloneScenario) -> dict:
        t = self.t
        a, b, c, d, e, f = t.transform
        h, w = t.height, t.width
        cols = np.arange(w)
        lons = c + (cols + 0.5) * a
        rows = np.arange(h)
        lats = f + (rows + 0.5) * e
        lon_g, lat_g = np.meshgrid(lons, lats)

        def haversine_km(lon1, lat1, lon2, lat2):
            dlon = np.radians(lon2 - lon1)
            dlat = np.radians(lat2 - lat1)
            aa = (
                np.sin(dlat / 2) ** 2
                + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
            )
            return 2 * 6371.0 * np.arcsin(np.sqrt(aa))

        r_km = haversine_km(scenario.landfall_lon, scenario.landfall_lat, lon_g, lat_g)
        rmw = scenario.radius_max_wind_km
        R = scenario.storm_radius_km
        vm = scenario.peak_wind_ms

        # Simple radial profile: rises to peak at RMW, decays to zero at R.
        wind = np.where(
            r_km >= R,
            0.0,
            np.where(
                r_km >= rmw,
                vm * np.clip(1.0 - (r_km - rmw) / max(R - rmw, 1.0), 0, 1) ** 1.2,
                vm * (0.55 + 0.45 * (r_km / rmw)),
            ),
        )

        # Surge: squared wind scaling, capped, decays inland. Distance inland
        # proxied by elevation + distance from the western AOI edge.
        surge_m = np.clip((wind / 70.0) ** 2 * 3.5, 0.0, 3.5)
        inland_km = haversine_km(scenario.landfall_lon, scenario.landfall_lat, lon_g, lat_g)
        surge_m *= np.clip(1.0 - inland_km / 25.0, 0.0, 1.0)

        surge_level = surge_m  # water level above mean sea level
        dem = np.where(np.isfinite(t.dem), t.dem, np.inf)
        surge_depth = np.clip(surge_level - dem, 0.0, None)

        wind_frac = np.clip((wind - 17.0) / (60.0 - 17.0), 0.0, 1.0)  # Beaufort gale..violent
        return {
            "wind_ms": wind.astype(np.float32),
            "surge_depth_m": surge_depth.astype(np.float32),
            "wind_frac": wind_frac.astype(np.float32),
            "peak_wind_ms": float(wind.max()),
            "max_surge_m": float(surge_m.max()),
            "scenario": scenario,
        }
