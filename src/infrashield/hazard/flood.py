"""Topography-aware flood simulation on the real DEM.

This is a *simplified but physically/topographically informed* flood model
(assumptions documented in docs/algorithm.md). It is deliberately NOT a
circular-buffer or static-radius flood: water is placed, moved and stored
according to the DEM's depressions, D8 drainage network and channel
discharge capacity.

Model outline
-------------
Inputs: hydrological terrain products (fill, D8 flow, accumulation,
depression storage curves), a rainfall scenario, optional sea-level offset
(tide / storm surge).

1. **Catchment concentration.** Every cell's D8 flow path (on the filled
   surface) is resolved to the depression it drains into (or to the sea).
   Rain falling on the whole catchment of a depression accumulates there —
   so valley bottoms fill from their full upstream area, not just locally.
2. **Staged depression filling.** The event is split into equal time steps.
   Each depression fills according to its hypsometric storage curve
   (volume -> water level). A depression that reaches capacity spills the
   excess over its rim: spill paths (rim cell -> downstream depression or
   sea) are precomputed from the D8 network, so cascades follow the terrain
   (upper depression fills -> overflows -> lower depression fills).
3. **Channel overflow.** Mapped channels (flow-accumulation network) carry
   discharge Q = rain intensity x upstream area. Where Q exceeds a Manning
   capacity estimate (bankfull cross-section), the surplus is spread as
   overbank depth around the channel — riverine flooding along the Mithi
   and other waterways even where no closed depression exists.
4. **Coastal offset.** An optional sea level (tide/surge) floods every cell
   below that level directly.

Output: per-stage depth grids (for the map animation), the final depth
grid, depression water levels and summary statistics. Infrastructure
exposure is computed from these actual depth fields at asset locations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from infrashield.hazard.terrain import TerrainData

logger = logging.getLogger(__name__)

G = 9.81


@dataclass
class FloodScenario:
    """Rainfall event definition (uniform intensity within the event)."""

    rainfall_mm: float          # total event rainfall depth
    duration_h: float           # event duration
    sea_level_m: float = 0.0    # tide / storm-surge water level (m.a.s.l.)
    n_stages: int = 8           # animation stages (and integration steps)
    name: str = "scenario"


@dataclass
class FloodResult:
    depths: np.ndarray                    # final depth (m), 0 = dry
    stages: list[np.ndarray]              # depth grids per stage
    stage_labels: list[str]               # e.g. "t + 45 min"
    depression_levels: dict[int, float]   # final water level per depression
    wet_fraction: float
    max_depth: float
    flood_volume_m3: float
    scenario: FloodScenario
    meta: dict = field(default_factory=dict)


class FloodSimulator:
    """Staged topographic flood model on preprocessed terrain."""

    def __init__(self, terrain: TerrainData, channel_roughness: float = 0.045) -> None:
        self.t = terrain
        self.roughness = channel_roughness
        self._dep_of_cell = np.full(terrain.height * terrain.width, -1, dtype=np.int64)
        for dep in terrain.depressions:
            self._dep_of_cell[dep["cells"]] = dep["id"]
        self._down_dep: np.ndarray | None = None
        self._spill_targets: dict[int, list[tuple[int, float]]] | None = None

    # ------------------------------------------------------------------ #
    # Catchment resolution
    # ------------------------------------------------------------------ #

    def _resolve_catchments(self) -> np.ndarray:
        """For each cell: id of the depression its D8 path drains into (-1 sea)."""
        if self._down_dep is not None:
            return self._down_dep
        t = self.t
        n = t.height * t.width
        recv = t.flow_dir.ravel()
        dep_of = self._dep_of_cell
        down = np.full(n, -2, dtype=np.int64)  # -2 = unresolved

        for i in range(n):
            if down[i] != -2:
                continue
            path: list[int] = []
            j = i
            res = -1
            while True:
                if dep_of[j] >= 0:      # inside a depression: drains into it
                    res = dep_of[j]
                    break
                if down[j] != -2:       # already resolved
                    res = down[j]
                    break
                r = recv[j]
                if r < 0:               # leaves the map -> sea
                    res = -1
                    break
                if dep_of[r] >= 0:      # receiver inside a depression
                    res = dep_of[r]
                    break
                path.append(j)
                j = r
                if len(path) > 10_000:  # pathological loop guard
                    res = -1
                    break
            for p in path:
                down[p] = res
        self._down_dep = down
        logger.info("Catchments resolved: %d cells", n)
        return down

    def _catchment_sizes(self) -> dict[int, int]:
        down = self._resolve_catchments()
        sizes: dict[int, int] = {}
        for dep in self.t.depressions:
            sizes[dep["id"]] = int((down == dep["id"]).sum())
        return sizes

    # ------------------------------------------------------------------ #
    # Spill routing (rim -> downstream depression)
    # ------------------------------------------------------------------ #

    def _resolve_spill_targets(self) -> dict[int, list[tuple[int, float]]]:
        """For each depression: [(downstream_depression, weight)] from rim cells."""
        if self._spill_targets is not None:
            return self._spill_targets
        t = self.t
        recv = t.flow_dir.ravel()
        dep_of = self._dep_of_cell
        down = self._resolve_catchments()
        targets: dict[int, list[tuple[int, float]]] = {}
        for dep in t.depressions:
            cells = dep["cells"]
            in_dep = set(cells.tolist())
            cell_set = in_dep
            rim_targets: dict[int, int] = {}
            for cell in cells:
                r0, c0 = divmod(int(cell), t.width)
                # rim cell: has a 4-neighbour outside the depression
                is_rim = False
                for dr, dc in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                    nr, nc = r0 + dr, c0 + dc
                    if 0 <= nr < t.height and 0 <= nc < t.width:
                        nidx = nr * t.width + nc
                        if nidx not in cell_set:
                            is_rim = True
                            break
                if not is_rim:
                    continue
                # walk downstream from the rim cell
                j = recv[int(cell)]
                steps = 0
                dest = -1
                while 0 <= j < len(dep_of) and steps < 20_000:
                    if dep_of[j] >= 0:
                        dest = int(dep_of[j])
                        break
                    if down[j] != -2 and down[j] >= 0:
                        dest = int(down[j])
                        break
                    j = recv[j]
                    steps += 1
                if dest >= 0:
                    rim_targets[dest] = rim_targets.get(dest, 0) + 1
            total = sum(rim_targets.values())
            targets[dep["id"]] = (
                [(d, cnt / total) for d, cnt in sorted(rim_targets.items())] if total else []
            )
        self._spill_targets = targets
        return targets

    # ------------------------------------------------------------------ #
    # Channel overflow
    # ------------------------------------------------------------------ #

    def _channel_overflow_depths(
        self, rain_ms: float, duration_s: float
    ) -> np.ndarray:
        """Riverine overbank flooding via floodplain corridor volume balance.

        For every channel cell the peak discharge is the rain intensity over
        its whole upstream catchment (D8 accumulation). Bankfull capacity is
        a Manning estimate for a small urban channel; the *surplus volume*
        is routed along the corridor of cells downstream of it whose ground
        is below the channel cell's bank crest (the valley the river would
        inundate), and spread as a level pool over that corridor by
        bisection on the water level. Depth is therefore deepest at the
        channel and follows the terrain — never a fixed radius.
        """
        t = self.t
        depth = np.zeros(t.height * t.width, dtype=np.float64)
        acc = t.flow_acc.ravel()
        chan = t.channel_mask.ravel()
        dem_flat = np.where(np.isfinite(t.dem), t.dem, np.inf).ravel()
        filled_flat = np.where(np.isfinite(t.filled), t.filled, np.inf).ravel()
        recv = t.flow_dir.ravel()
        idxs = np.flatnonzero(chan & np.isfinite(t.dem.ravel()))
        if idxs.size == 0:
            return depth.reshape(t.height, t.width)
        for i in idxs:
            a = acc[i]
            if a < 50:  # only meaningful channels overflow
                continue
            discharge = rain_ms * a * t.cell_area_m2          # m3/s peak
            width = min(30.0, 4.0 + 2.0 * np.sqrt(a))
            incision = 3.0
            area_x = width * incision
            perimeter = 2.0 * width + 2.0 * incision
            radius = area_x / perimeter
            r0, c0 = divmod(int(i), t.width)
            slope = max(float(t.slope[r0, c0]), 1e-4)
            capacity = (1.0 / self.roughness) * area_x * radius ** (2 / 3) * np.sqrt(slope)
            surplus = max(0.0, discharge - capacity) * duration_s  # m3/event
            if surplus <= 0:
                continue
            # Corridor: downstream cells whose ground stays below this
            # channel's bank crest (they'd be inundated before the bank
            # holds). Cap corridor length to keep locality.
            crest = float(filled_flat[i])
            corridor: list[int] = [i]
            j = int(recv[i])
            steps = 0
            while j >= 0 and steps < 400:
                if dem_flat[j] < crest + 0.5:
                    corridor.append(j)
                j = int(recv[j])
                steps += 1
            zs = np.array([dem_flat[c] for c in corridor])
            order = np.argsort(zs)
            zs_s = zs[order]
            area = t.cell_area_m2
            k_idx = np.arange(1, zs_s.size + 1)
            vol_at = (k_idx * zs_s - zs_s.cumsum()) * area
            if surplus >= vol_at[-1]:
                level = float(zs_s[-1])
            else:
                lo, hi = 0, vol_at.size - 1
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    if vol_at[mid] < surplus:
                        lo = mid
                    else:
                        hi = mid
                v_lo = vol_at[lo]
                z_lo, z_hi = zs_s[lo], zs_s[hi]
                level = float(z_lo + (z_hi - z_lo) * (surplus - v_lo) / max(vol_at[hi] - v_lo, 1e-9))
            for c in corridor:
                d = level - dem_flat[c]
                if d > 0:
                    depth[c] = max(depth[c], d)
        return depth.reshape(t.height, t.width)

    # ------------------------------------------------------------------ #
    # Main run
    # ------------------------------------------------------------------ #

    def _level_for_volume(self, dep: dict, volume: float) -> float:
        """Absolute water level for a stored volume (exact hypsometric interp)."""
        z = dep["z_sorted"]
        vol_at = dep["vol_at_level"]
        if volume <= 0 or z.size == 0:
            return 0.0
        if volume >= dep["capacity_m3"]:
            return float(dep["rim_elev"])
        k = int(np.searchsorted(vol_at, volume, side="right"))
        if k >= z.size:
            return float(dep["rim_elev"])
        v_lo = vol_at[k - 1] if k > 0 else 0.0
        z_lo = z[k - 1] if k > 0 else z[0]
        z_hi = z[k]
        v_hi = vol_at[k]
        if v_hi <= v_lo:
            return float(z_hi)
        frac = (volume - v_lo) / (v_hi - v_lo)
        return float(z_lo + frac * (z_hi - z_lo))

    def run(self, scenario: FloodScenario) -> FloodResult:
        t = self.t
        duration_s = max(scenario.duration_h * 3600.0, 1.0)
        rain_ms = scenario.rainfall_mm / 1000.0 / duration_s
        k = max(1, scenario.n_stages)
        catch = self._catchment_sizes()
        spills = self._resolve_spill_targets()
        dem_flat = np.where(np.isfinite(t.dem), t.dem, np.inf).ravel()

        dep_by_id = {d["id"]: d for d in t.depressions}
        v_max = {i: float(d["capacity_m3"]) for i, d in dep_by_id.items()}
        # Total rain volume a depression receives over the whole event:
        # rain intensity x catchment area x duration.
        v_own_total = {i: rain_ms * t.cell_area_m2 * catch.get(i, 0) * duration_s
                       for i in dep_by_id}

        vol_in = {i: 0.0 for i in dep_by_id}
        levels = {i: 0.0 for i in dep_by_id}
        stage_levels: list[dict[int, float]] = []
        pending: dict[int, float] = {}

        for _ in range(k):
            for i in dep_by_id:
                vol = vol_in[i] + v_own_total[i] / k + pending.get(i, 0.0)
                if vol > v_max[i] > 0:
                    excess = vol - v_max[i]
                    vol_in[i] = v_max[i]
                    for target, weight in spills.get(i, []):
                        pending[target] = pending.get(target, 0.0) + excess * weight
                else:
                    vol_in[i] = max(vol, 0.0)
                levels[i] = self._level_for_volume(dep_by_id[i], vol_in[i])
            stage_levels.append(dict(levels))
            pending = {}

        chan_depth = self._channel_overflow_depths(rain_ms, duration_s)
        chan_max = float(chan_depth.max()) if chan_depth.size else 0.0
        # Spread channel overbank growth over the stages (quadratic approach:
        # slow start, accelerating — like a real hydrograph rising limb) and
        # ramp the surge in over the first 3/4 of the event.
        chan_stage = [chan_depth * ((si + 1) / k) ** 2 for si in range(k)]
        surge_stage = [
            scenario.sea_level_m * ((si + 1) / k) ** 1.5 for si in range(k)
        ]

        stages: list[np.ndarray] = []
        stage_labels: list[str] = []
        for si, lv in enumerate(stage_levels):
            depth = np.zeros(t.height * t.width, dtype=np.float64)
            for i, level in lv.items():
                if level <= 0:
                    continue
                cells = dep_by_id[i]["cells"]
                d = np.clip(level - dem_flat[cells], 0.0, None)
                np.maximum(depth[cells], d, out=depth[cells])
            depth += chan_stage[si].ravel()
            if surge_stage[si] > 0:
                coastal = np.clip(surge_stage[si] - dem_flat, 0.0, None)
                np.maximum(depth, coastal, out=depth)
            depth = depth.reshape(t.height, t.width)
            depth[~np.isfinite(depth)] = 0.0
            stages.append(depth.astype(np.float32))
            minutes = int(round(scenario.duration_h * 60 * (si + 1) / k))
            stage_labels.append(f"t + {minutes} min" if minutes < 60 else f"t + {minutes / 60:.1f} h")

        final = stages[-1]
        wet = final > 0.05
        volume = float((np.where(wet, final, 0.0) * t.cell_area_m2).sum())
        logger.info(
            "Flood run: rain=%.0fmm/%.1fh surge=%.1fm -> wet %.1f%% max %.2fm",
            scenario.rainfall_mm, scenario.duration_h, scenario.sea_level_m,
            100 * wet.mean(), float(final.max()),
        )
        return FloodResult(
            depths=final,
            stages=stages,
            stage_labels=stage_labels,
            depression_levels=levels,
            wet_fraction=float(wet.mean()),
            max_depth=float(final.max()),
            flood_volume_m3=volume,
            scenario=scenario,
            meta={
                "n_depressions": len(t.depressions),
                "catchment_cells": catch,
                "channel_overflow_max_m": chan_max,
                "channel_overflow_volume_m3": float(
                    (chan_depth * t.cell_area_m2).sum()
                ),
            },
        )
