"""Terrain analysis and hydrological preprocessing on the real DEM.

Pipeline (standard practice, cf. Planchon-Darboux / Wang-Liu fill, D8):

1. Fill: raise cells inside depressions to the level of their lowest
   rim outlet ("sink filling"). The *fill depth* ``filled - dem`` marks
   natural depressions and flat valleys where water accumulates.
2. D8 flow directions: each cell drains to its steepest-descent neighbour
   on the filled surface (flats resolved by the first available descent).
3. Flow accumulation: number of upstream cells draining through each cell
   (topological order by filled elevation). Channels = cells whose
   accumulation exceeds ``channel_threshold`` cells (~a mapped stream).
4. Depression storage: for each fill-depth contour, the volume of water it
   can hold before spilling over its rim (trapezoid integration of the
   hypsometric curve area(dh)), plus the rim spill elevation and the set of
   rim cells through which overflow leaves the depression.

Assumptions are documented in docs/algorithm.md.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np

# D8 neighbour offsets and the horizontal distance of each step (cell units).
# Order: E, NE, N, NW, W, SW, S, SE.
D8_OFFSETS = [(0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1), (1, 0), (1, 1)]
D8_DIST = [1.0, 2**0.5, 1.0, 2**0.5, 1.0, 2**0.5, 1.0, 2**0.5]


@dataclass
class TerrainData:
    """All gridded products of the hydrological preprocessing."""

    dem: np.ndarray            # raw elevation (m), nodata -> nan
    filled: np.ndarray         # depression-filled elevation (m)
    fill_depth: np.ndarray     # filled - dem (m); depressions & flats
    flow_dir: np.ndarray       # D8 receiver index per cell (-1 = drains off-map)
    flow_acc: np.ndarray       # upstream cell count (int)
    slope: np.ndarray          # |gradient| from the filled surface (m/m)
    cell_area_m2: float        # cell footprint in m2
    channel_mask: np.ndarray   # bool: flow_acc >= channel_threshold
    depressions: list[dict]    # per-depression metadata (storage, rim, ...)
    transform: tuple           # rasterio-style affine (a, b, c, d, e, f)
    crs: str
    height: int
    width: int

    def rowcol_to_lonlat(self, row: int, col: int) -> tuple[float, float]:
        a, b, c, d, e, f = self.transform
        x = c + (col + 0.5) * a
        y = f + (row + 0.5) * e
        return x, y  # x=lon, y=lat for EPSG:4326 rasters


def load_dem(path) -> tuple[np.ndarray, tuple, str]:
    """Read the DEM raster; nodata becomes nan."""
    import rasterio

    with rasterio.open(path) as src:
        dem = src.read(1).astype("float64")
        transform = src.transform.to_gdal() if hasattr(src.transform, "to_gdal") else src.transform
        # rasterio Affine -> (a, b, xoff, d, e, yoff) 6-tuple (GDAL order)
        a = src.transform.a
        b = src.transform.b
        c = src.transform.c
        d = src.transform.d
        e = src.transform.e
        f = src.transform.f
        transform = (a, b, c, d, e, f)
        crs = str(src.crs)
        nodata = src.nodata
    if nodata is not None:
        dem[dem == nodata] = np.nan
    dem[dem < -1000] = np.nan
    return dem, transform, crs


def fill_depressions(dem: np.ndarray) -> np.ndarray:
    """Priority-flood depression filling (Wang & Liu style).

    Starting from the map edge, always process the lowest known cell first;
    any interior cell lower than the running spill level is raised to it.
    """
    h, w = dem.shape
    filled = dem.copy()
    filled[np.isnan(filled)] = np.inf
    visited = np.zeros((h, w), dtype=bool)
    heap: list[tuple[float, int, int]] = []

    for r in range(h):
        for c in (0, w - 1):
            if not visited[r, c]:
                visited[r, c] = True
                heapq.heappush(heap, (filled[r, c], r, c))
    for c in range(w):
        for r in (0, h - 1):
            if not visited[r, c]:
                visited[r, c] = True
                heapq.heappush(heap, (filled[r, c], r, c))

    while heap:
        elev, r, c = heapq.heappop(heap)
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and not visited[nr, nc]:
                visited[nr, nc] = True
                if filled[nr, nc] < elev:
                    filled[nr, nc] = elev
                heapq.heappush(heap, (filled[nr, nc], nr, nc))
    return filled


def d8_flow_directions(filled: np.ndarray, cell_size_m: float) -> np.ndarray:
    """D8: receiver index per cell (into flattened arrays), -1 = none.

    Pass 1: steepest descent among strictly lower neighbours.
    Pass 2 (flat resolution): after depression filling, flats have no
    strictly-lower neighbour. A multi-source BFS seeded from all cells that
    already drain spreads *across equal-elevation flats*, pointing each flat
    cell toward the nearest cell with an existing downhill route (cf.
    Garbrecht & Martz flat resolution, simplified). Without this, filled
    depressions would dead-end and never contribute downstream flow.
    """
    h, w = filled.shape
    flat = filled.ravel()
    recv = np.full(h * w, -1, dtype=np.int64)
    for r in range(h):
        for c in range(w):
            idx = r * w + c
            here = flat[idx]
            if not np.isfinite(here):
                continue
            best_slope = 0.0
            best_idx = -1
            for k, (dr, dc) in enumerate(D8_OFFSETS):
                nr, nc = r + dr, c + dc
                if not (0 <= nr < h and 0 <= nc < w):
                    continue
                nidx = nr * w + nc
                drop = here - flat[nidx]
                slope = drop / (D8_DIST[k] * cell_size_m)
                if slope > best_slope:
                    best_slope = slope
                    best_idx = nidx
            recv[idx] = best_idx

    # Flat resolution BFS: from draining cells across equal-height neighbours.
    from collections import deque

    queue = deque(int(i) for i in np.flatnonzero(recv >= 0))
    while queue:
        idx = queue.popleft()
        r, c = divmod(idx, w)
        here = flat[idx]
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < h and 0 <= nc < w):
                continue
            nidx = nr * w + nc
            if recv[nidx] != -1 or not np.isfinite(flat[nidx]):
                continue
            if flat[nidx] == here:  # same-elevation flat cell
                recv[nidx] = idx    # drain toward the already-routed cell
                queue.append(nidx)
    return recv


def flow_accumulation(filled: np.ndarray, flow_dir: np.ndarray) -> np.ndarray:
    """Upstream cell counts via Kahn topological peel of the D8 DAG.

    Peeling zero-donor cells guarantees every donor contributes before its
    receiver is processed -- correct even across filled flats.
    """
    from collections import deque

    n = filled.size
    recv = flow_dir
    acc = np.ones(n, dtype=np.float64)
    indeg = np.zeros(n, dtype=np.int64)
    for i in range(n):
        j = recv[i]
        if j >= 0:
            indeg[j] += 1
    queue = deque(int(i) for i in np.flatnonzero(indeg == 0))
    processed = 0
    while queue:
        i = queue.popleft()
        processed += 1
        j = recv[i]
        if j >= 0:
            acc[j] += acc[i]
            indeg[j] -= 1
            if indeg[j] == 0:
                queue.append(j)
    if processed < n:  # cycles cannot occur (strict descent or BFS-to-outlet)
        logger.warning("Flow accumulation: %d cells unreachable (cycles?)", n - processed)
    return acc.reshape(filled.shape)


def compute_slope(filled: np.ndarray, cell_size_m: float) -> np.ndarray:
    """|gradient| via central differences on the filled surface (m per m)."""
    h, w = filled.shape
    slope = np.zeros((h, w))
    safe = np.where(np.isfinite(filled), filled, np.nanmean(filled))
    gy, gx = np.gradient(safe, cell_size_m)
    slope = np.hypot(gx, gy)
    return slope


def identify_depressions(
    dem: np.ndarray,
    filled: np.ndarray,
    cell_area_m2: float,
    max_depressions: int = 2000,
) -> list[dict]:
    """Segment fill-depth > 0 into depressions with exact storage curves.

    For each depression we store the sorted cell elevations; the water level
    for a stored volume follows from the exact hypsometric relation
    ``V(L) = sum_{z < L} (L - z) * cell_area`` (monotone in L), including the
    storage *above* the original ground surface up to the rim (shallow
    pans mostly store water above ground, not below). ``rim_elev`` is the
    filled-surface spill level; water never rises above it (excess spills).
    """
    h, w = dem.shape
    depth = filled - dem
    labels = np.zeros((h, w), dtype=np.int32)
    from scipy import ndimage as ndi  # local import; scipy ships with rasterio

    mask = depth > 1e-6
    lab, n = ndi.label(mask)
    depressions: list[dict] = []
    if n == 0:
        return depressions

    sizes = ndi.sum(mask, lab, index=range(1, n + 1))
    order = np.argsort(-sizes)[:max_depressions]
    for di in order:
        di = int(di) + 1
        sel = lab == di
        dem_vals = dem[sel]
        bottom = float(np.nanmin(dem_vals))
        rim = float(np.nanmax(filled[sel]))
        # Exact hypsometric curve from sorted cell elevations. To represent
        # storage *above* ground too (water level > cell elevation), append
        # a rim-height proxy cell whose "bowl" extends `rim` metres above:
        # the curve is then exact up to the rim (searchsorted stays valid).
        z = np.sort(dem_vals)
        k_idx = np.arange(1, z.size + 1)
        cum = z.cumsum()
        vol_at_z = (k_idx * z - cum) * cell_area_m2
        capacity = float((rim - z).sum() * cell_area_m2)
        # Extra curve point so volumes between vol_at_z[-1] and capacity map
        # monotonically to levels up to the rim.
        z_ext = np.append(z, rim)
        vol_ext = np.append(vol_at_z, capacity)
        depressions.append(
            {
                "id": len(depressions),
                "cells": np.flatnonzero(sel.ravel()),
                "bottom": bottom,
                "rim_elev": rim,
                "max_depth": float(np.nanmax(depth[sel])),
                "z_sorted": z_ext,
                "vol_at_level": vol_ext,
                "capacity_m3": capacity,
                "n_cells": int(sel.sum()),
            }
        )
    return depressions


def preprocess(path, channel_threshold: int = 250) -> TerrainData:
    """Full hydrological preprocessing of a DEM file."""
    dem, transform, crs = load_dem(path)
    a, b, c, d, e, f = transform
    # The raster is in EPSG:4326 (square degree cells); convert to metres at
    # the AOI's mean latitude (1 deg lat ~ 111.32 km).
    lat_center = f + dem.shape[0] * e / 2.0
    cell_size_m = abs(a) * 111_320.0 * np.cos(np.deg2rad(lat_center))

    filled = fill_depressions(dem)
    flow_dir = d8_flow_directions(filled, cell_size_m)
    flow_acc = flow_accumulation(filled, flow_dir)
    slope = compute_slope(filled, cell_size_m)
    cell_area_m2 = cell_size_m**2
    channel_mask = flow_acc >= channel_threshold
    depressions = identify_depressions(dem, filled, cell_area_m2)

    return TerrainData(
        dem=dem,
        filled=filled,
        fill_depth=filled - dem,
        flow_dir=flow_dir,
        flow_acc=flow_acc,
        slope=slope,
        cell_area_m2=cell_area_m2,
        channel_mask=channel_mask,
        depressions=depressions,
        transform=transform,
        crs=crs,
        height=dem.shape[0],
        width=dem.shape[1],
    )
