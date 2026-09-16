"""Landslide susceptibility field on the real DEM.

Deterministic and explainable (assumptions documented in docs/algorithm.md):

* **Slope control**: susceptibility rises with terrain slope above a
  configurable threshold angle (default 15 deg) and saturates at a critical
  angle (default 35 deg) — the classic infinite-slope stability picture.
  Everything below the threshold is inherently stable (susceptibility 0).
* **Rainfall / soil-moisture proxy**: without pore-pressure measurements the
  triggering stress is proxied by the scenario rainfall: an antecedent
  factor 0.5..1.0 scales the susceptibility (dry hillslopes need much more
  rain to fail; the 600 mm / 24 h mark counts as fully saturating). This is
  the standard rainfall-threshold simplification, not a hydrological model.

The result is a susceptibility index 0..1 per DEM cell; exposure sampling,
risk scoring (H = 10 x susceptibility) and the animation all consume it
exactly like the other hazard fields.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from infrashield.hazard.terrain import TerrainData

# Slope controls (degrees). Tunable; documented in docs/algorithm.md.
SLOPE_THRESHOLD_DEG = 15.0   # below this, hillslopes are inherently stable
SLOPE_CRITICAL_DEG = 35.0    # at/above this, full susceptibility (pre-rain)
RAIN_SATURATION_MM = 600.0   # rainfall that counts as fully saturating


@dataclass
class LandslideScenario:
    slope_threshold_deg: float = SLOPE_THRESHOLD_DEG
    rainfall_mm: float = 300.0
    duration_h: float = 24.0
    name: str = "landslide"


class LandslideSimulator:
    """Static susceptibility field (single stage, like heat)."""

    def __init__(self, terrain: TerrainData) -> None:
        self.t = terrain

    def run(self, scenario: LandslideScenario) -> dict:
        t = self.t
        # terrain.slope is |gradient| in m/m from the *filled* surface —
        # exactly the ground surface water would mobilise.
        slope = np.where(np.isfinite(t.slope), t.slope, 0.0)
        slope_deg = np.degrees(np.arctan(slope))

        thr = float(scenario.slope_threshold_deg)
        crit = max(thr + 5.0, SLOPE_CRITICAL_DEG)
        # 0 below threshold -> 1 at critical angle, linear in between.
        slope_term = np.clip((slope_deg - thr) / (crit - thr), 0.0, 1.0)

        # Rainfall/soil-moisture proxy: 0.5 (dry) .. 1.0 (saturated).
        rain = max(0.0, float(scenario.rainfall_mm))
        rain_factor = 0.5 + 0.5 * min(1.0, rain / RAIN_SATURATION_MM)

        sus = np.clip(slope_term * rain_factor, 0.0, 1.0).astype(np.float32)

        return {
            "susceptibility": sus,
            "susceptible_fraction": float((sus > 0.05).mean()),
            "max_susceptibility": float(sus.max()),
            "slope_threshold_deg": thr,
            "rain_factor": round(rain_factor, 3),
            "scenario": scenario,
        }
