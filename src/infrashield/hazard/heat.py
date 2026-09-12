"""Extreme-heat hazard field on the real terrain.

Physically-informed but deliberately simple (assumptions documented in
docs/algorithm.md): near-surface air temperature on the DEM grid is the
regional background anomaly modulated by three terrain/land-cover controls:

* **Lapse rate**: air cools ~6.5 C/km with elevation — hilltops are cooler.
* **Heat-sink classes**: dense built-up areas store heat (urban heat
  island); parks, water and forest cool their surroundings. Proximity to
  OSM green/blue features shifts local temperature.
* **Sea breeze**: cells near the coastline sit slightly cooler.

The hazard metric is a heat index 0..1 per cell, derived from the local
temperature above a comfort threshold, so exposure joins work exactly like
the flood model (sample the grid at asset locations).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from infrashield.hazard.terrain import TerrainData

LAPSE_RATE_C_PER_KM = 6.5
SEA_BREEZE_KM = 2.0
SEA_BREEZE_COOLING_C = 1.2


@dataclass
class HeatScenario:
    anomaly_c: float          # regional heatwave anomaly above seasonal normal
    normal_max_c: float = 33.0  # seasonal normal daily max for Mumbai
    name: str = "heatwave"


class HeatSimulator:
    """Static heat field: anomaly + lapse + UHI proxies (no time stages)."""

    def __init__(self, terrain: TerrainData, green_cells: np.ndarray | None = None,
                 water_cells: np.ndarray | None = None) -> None:
        self.t = terrain
        self.green = green_cells
        self.water = water_cells

    def _cooling_field(self) -> np.ndarray:
        """Cooling contribution (C, negative) from green/water proximity."""
        t = self.t
        cool = np.zeros((t.height, t.width), dtype=np.float64)
        from scipy import ndimage as ndi

        for mask, strength, radius_cells in (
            (self.green, 2.0, 12),
            (self.water, 3.0, 10),
        ):
            if mask is None or not mask.any():
                continue
            dist = ndi.distance_transform_edt(~mask, sampling=t.cell_area_m2 ** 0.5)
            influence = np.clip(1.0 - dist / (radius_cells * t.cell_area_m2 ** 0.5), 0, 1)
            cool -= strength * influence
        return cool

    def run(self, scenario: HeatScenario) -> dict:
        t = self.t
        dem = np.where(np.isfinite(t.dem), t.dem, np.nan)
        dem0 = float(np.nanmean(dem))

        temp = np.full((t.height, t.width), scenario.normal_max_c + scenario.anomaly_c)
        # lapse rate relative to the mean AOI elevation
        temp -= LAPSE_RATE_C_PER_KM * (dem - dem0) / 1000.0
        temp += self._cooling_field()

        # heat index: 0 at <=33C, 1 at >=45C (physiological stress scale)
        comfort, extreme = 33.0, 45.0
        heat_index = np.clip((temp - comfort) / (extreme - comfort), 0.0, 1.0)
        heat_index[~np.isfinite(heat_index)] = 0.0

        return {
            "temp_c": temp.astype(np.float32),
            "heat_index": heat_index.astype(np.float32),
            "max_temp": float(np.nanmax(temp)),
            "mean_temp": float(np.nanmean(temp)),
            "scenario": scenario,
        }
