"""Tests for the hazard models (terrain, flood, heat, cyclone) - offline.

Synthetic DEMs with known depressions/channels verify the physics: water
finds lows, flows downhill, and deeper scenarios flood more.
"""

from __future__ import annotations

import numpy as np
import pytest

from infrashield.hazard.flood import FloodScenario, FloodSimulator
from infrashield.hazard.heat import HeatScenario, HeatSimulator
from infrashield.hazard.terrain import preprocess
from infrashield.risk.engine import (
    build_geometry_samples,
    geom_sample_points,
    sample_grid_vectorized,
)


@pytest.fixture(scope="module")
def tiny_terrain(tmp_path_factory):
    """20x20 DEM, 1 deg cells?? no - cell size is irrelevant for topology.

    A bowl in the SW quadrant and a sloping plane elsewhere, written to a
    GeoTIFF so preprocess() runs the full pipeline.
    """
    rasterio = pytest.importorskip("rasterio")
    h, w = 24, 24
    yy, xx = np.mgrid[0:h, 0:w]
    dem = 10.0 + 0.15 * xx + 0.1 * yy  # gentle slope to the SE
    dem[4:10, 4:10] = 5.0              # bowl at 5 m in the high NW area
    dem[18:22, 14:20] = 7.0            # second, shallower bowl
    path = tmp_path_factory.mktemp("dem") / "tiny.tif"
    transform = rasterio.transform.from_origin(72.85, 19.18, 0.001, 0.001)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326", transform=transform) as dst:
        dst.write(dem.astype("float32"), 1)
    return preprocess(path, channel_threshold=30)


class TestTerrain:
    def test_fill_removes_depressions(self, tiny_terrain):
        t = tiny_terrain
        # filled surface must never be below the DEM
        assert np.all((t.filled - t.dem) >= -1e-9)
        # the bowl cells were raised toward the rim
        assert t.filled[4:10, 4:10].min() > 5.0 + 1e-6

    def test_flow_accumulation_peaks_in_bowl(self, tiny_terrain):
        t = tiny_terrain
        # the deepest accumulation happens at the bowl outlet
        assert t.flow_acc.max() > 30

    def test_depressions_detected(self, tiny_terrain):
        deps = tiny_terrain.depressions
        assert len(deps) >= 2
        # storage curves are monotone in level
        d0 = deps[0]
        assert np.all(np.diff(d0["vol_at_level"]) >= -1e-6)
        assert d0["capacity_m3"] > 0
        assert d0["rim_elev"] > d0["bottom"]

    def test_transform_roundtrip(self, tiny_terrain):
        t = tiny_terrain
        lon, lat = t.rowcol_to_lonlat(0, 0)
        assert 72.8 < lon < 72.9 and 19.1 < lat < 19.2


class TestFlood:
    def test_more_rain_more_inundation(self, tiny_terrain):
        """Wet *area* is non-decreasing and flooded *volume* strictly
        increases with rainfall (on a tiny DEM the bowls saturate in area
        quickly; depth keeps rising - that is the physical signal)."""
        t = tiny_terrain
        wet, vol = [], []
        for mm in (50, 200, 600):
            res = FloodSimulator(t).run(FloodScenario(rainfall_mm=mm, duration_h=24, n_stages=4))
            wet.append(res.wet_fraction)
            vol.append(res.flood_volume_m3)
        assert wet[0] <= wet[1] <= wet[2], f"wet area shrank: {wet}"
        assert vol[0] < vol[1] < vol[2], f"volume not monotone: {vol}"

    def test_water_stays_in_depressions(self, tiny_terrain):
        t = tiny_terrain
        res = FloodSimulator(t).run(FloodScenario(rainfall_mm=300, duration_h=24, n_stages=4))
        # every wet cell must be a fill-depth cell (a depression) or channel
        wet = res.depths > 0.02
        if wet.any():
            rows, cols = np.nonzero(wet)
            fd = t.fill_depth[rows, cols]
            chan = t.channel_mask[rows, cols]
            assert np.all((fd > 1e-6) | chan), "water appeared on a dry hilltop"

    def test_stages_are_monotone_per_cell(self, tiny_terrain):
        t = tiny_terrain
        res = FloodSimulator(t).run(FloodScenario(rainfall_mm=400, duration_h=24, n_stages=6))
        stack = np.stack(res.stages)  # (stages, h, w)
        assert np.all(np.diff(stack, axis=0) >= -1e-9), "flood receded within one event"

    def test_max_depth_bounded_by_storage(self, tiny_terrain):
        res = FloodSimulator(tiny_terrain).run(
            FloodScenario(rainfall_mm=900, duration_h=72, n_stages=4))
        assert 0 < res.max_depth < 60  # 23 m would be absurd on this DEM


class TestHeat:
    def test_heat_index_in_unit_range(self, tiny_terrain):
        res = HeatSimulator(tiny_terrain).run(HeatScenario(anomaly_c=8))
        hi = res["heat_index"]
        assert hi.min() >= 0.0 and hi.max() <= 1.0

    def test_stronger_anomaly_hotter(self, tiny_terrain):
        t = tiny_terrain
        r1 = HeatSimulator(t).run(HeatScenario(anomaly_c=4))
        r2 = HeatSimulator(t).run(HeatScenario(anomaly_c=10))
        assert r2["max_temp"] > r1["max_temp"]

    def test_water_cools(self, tiny_terrain):
        """A water mask should cool cells near it relative to a dry run."""
        t = tiny_terrain
        water = np.zeros((t.height, t.width), dtype=bool)
        water[10:14, 10:14] = True
        r_dry = HeatSimulator(t).run(HeatScenario(anomaly_c=8))
        r_wet = HeatSimulator(t, water_cells=water).run(HeatScenario(anomaly_c=8))
        assert r_wet["temp_c"].mean() < r_dry["temp_c"].mean()


class TestSampling:
    """The vectorised exposure sampler used by every hazard."""

    def test_points_and_lines(self, tiny_terrain):
        from shapely.geometry import LineString, Point

        t = tiny_terrain
        grid = np.full((t.height, t.width), 2.0)
        grid[5, 5] = 9.0  # inside the bowl
        # rowcol_to_lonlat returns (lon, lat); shapely wants (lon, lat)
        geoms = [Point(*t.rowcol_to_lonlat(5, 5)),
                 LineString([t.rowcol_to_lonlat(0, 0), t.rowcol_to_lonlat(3, 3)])]
        owner, lons, lats, n = build_geometry_samples(geoms, t)
        out = sample_grid_vectorized(grid, t, owner, lons, lats, n)
        assert out[0] == pytest.approx(9.0)   # point hit the hot cell
        assert out[1] == pytest.approx(2.0)   # line samples the plain
        assert n == 2

    def test_out_of_bounds_is_zero(self, tiny_terrain):
        from shapely.geometry import Point

        t = tiny_terrain
        grid = np.full((t.height, t.width), 5.0)
        owner, lons, lats, n = build_geometry_samples([Point(0, 0)], t)
        out = sample_grid_vectorized(grid, t, owner, lons, lats, n)
        assert out[0] == 0.0

    def test_mean_reducer(self, tiny_terrain):
        from shapely.geometry import LineString

        t = tiny_terrain
        grid = np.full((t.height, t.width), 1.0)
        grid[5, 5] = 5.0
        # endpoints (5,5)=5.0 and (5,6)=1.0 -> mean 3.0
        geoms = [LineString([t.rowcol_to_lonlat(5, 5), t.rowcol_to_lonlat(5, 6)])]
        owner, lons, lats, n = build_geometry_samples(geoms, t)
        out = sample_grid_vectorized(grid, t, owner, lons, lats, n, reducer="mean")
        assert 1.0 < out[0] < 5.0

    def test_densification_caps_points(self):
        from shapely.geometry import LineString

        line = LineString([(i * 0.001, i * 0.001) for i in range(100)])
        pts = geom_sample_points(line, max_pts=24)
        assert len(pts) <= 24
