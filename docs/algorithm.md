# InfraShield risk & cascading algorithm — design record

This document records (1) what the repository implemented before this change,
(2) how it compares to the required algorithm, (3) what was implemented now,
and (4) every simplifying assumption in the physics — so results are never
mistaken for calibrated hydrology or wind engineering.

---

## 1. What existed before

Phase 1 of InfraShield implemented the **data pipeline only**:

| Component | State before | Verdict after review |
| --- | --- | --- |
| OSM fetch (Overpass, 10 categories, cached) | working, tested | **kept** |
| GeoJSON processing (clean, clip, length) | working, tested | **kept** |
| PostGIS schema (`infra_asset`, `infra_dependency`, `asset_risk_score`, `hazard_event`) | DDL only, unused | **kept** (optional store) |
| `graph/dependencies.py` | proximity dependency graph + `cascade_from` weighted BFS decay | **kept and reused** — sound design, now feeds the risk engine |
| Risk / exposure / hazard models | **none** | built now |
| Road-network routing & accessibility | **none** | built now |
| UI | **none** | built now |

### Decision: which algorithm?

The brief asked to compare the existing logic against the required algorithm and
keep the better one. The existing logic covered exactly one ingredient —
dependency-edge weighting and `cascade_from` (BFS with `strength × weight`
decay and a 0.05 floor). That design is *good*: it is multi-source-safe,
bounded, and its decay semantics match the required "substation failure →
power-dependent facility affected" chain. It was therefore **kept verbatim as
the propagation kernel** (`infrashield/risk/cascade.py` wraps it with a
max-strength Dijkstra equivalent that is O(V+E) for any number of failing
assets, using the same weights and decay floor).

Everything else the brief requires (hazard generation, topographic flood,
exposure, vulnerability, criticality, risk = E×V×C, network impact, priority
scoring) **did not exist** in any form, so there was nothing to compare — it
was implemented fresh on top of the real DEM and OSM data.

## 2. The algorithm as implemented

```
            ┌──────────────── data (all real) ────────────────┐
            │ Copernicus GLO-30 DEM (30 m)   OSM asset layers  │
            └───────────────────────┬─────────────────────────┘
                                    ▼
 1. terrain.py    fill depressions → D8 flow dirs → flow accumulation
                  → channels → depression storage curves (hypsometric)
 2. hazard model  flood.py   depression level-pool routing + channel
                             overbank + coastal surge, per time stage
                  heat.py    background anomaly − lapse rate + UHI/built-up
                             heating + water cooling
                  cyclone.py parametric wind field (Holland-style) +
                             DEM-clipped surge inundation
 3. engine.py     exposure sampled at each asset's real geometry
                  (points: containing cell; lines/areas: max over ≤24
                  densified samples) → risk = exposure × vuln × crit
 4. network.py    road graph from OSM geometries; edges fail at usability
                  ≥ 0.35 (flood depth / wind / heat exposure); detour
                  minutes per failed edge; multi-source Dijkstra
                  accessibility increase to hospitals
 5. cascade.py    failure of assets with risk ≥ 0.45 propagates through
                  the dependency graph (edge weights, 0.05 decay floor)
 6. scenario.py   impact = 0.30·criticality + 0.30·network + 0.20·cascade
                  + 0.15·population + 0.05·redundancy-loss
                  priority = max(risk, impact) + 0.3·min(risk, impact)
```

**Risk vs impact, deliberately separated** (as the brief requires):

* **risk** — likelihood/severity that the asset itself fails: `exposure ×
  vulnerability × criticality`, where vulnerability rises for tunnels,
  bridges (scour), cuttings, and low-lying ground (DEM-derived).
* **impact** — consequence *if* it fails: criticality, road-network
  disruption it causes, cascade footprint, population served, redundancy
  lost.
* **priority** blends both, dominated by the larger: a barely exposed
  substation can outrank a flooded footpath.

## 3. Flood model: what is real, what is assumed

**Real (from data):**
- Elevation from Copernicus GLO-30 at ~29 m posting (resampled 30 m→469×541 grid).
- Depression geometry, rims, and exact storage curves `V(L)` from the DEM itself.
- Flow directions and accumulation from the filled surface (D8; flats resolved by
  BFS toward existing outlets; accumulation by Kahn topological peel).
- Channels = accumulation ≥ 250 upstream cells; catchment areas per depression
  from D8 paths; rim spill topology for inter-depression overflow.

**Simplified (documented assumptions):**
1. **Level-pool, no dynamics** — each stage is a water balance, not a
   hydrodynamic solve. Within-stage propagation time is ignored; stages show
   *where* water goes, in a plausible order, not a calibrated arrival time.
2. **Uniform rainfall** over the AOI (no radar/gauge forcing); rain → volume
   via `intensity × catchment area × duration`, no infiltration/evaporation
   partitioning (conservative).
3. **Channel capacity** from a Manning estimate with an assumed small urban
   channel geometry (width from accumulation, 3 m incision, n=0.045); real
   cross-sections are not in OSM.
4. **No drainage/sewer network** — OSM has none; pluvial flooding is
   therefore overestimated in well-drained areas, underestimated where
   sewers back up.
5. **Coastal surge** is a user-specified sea-level rise applied where
   `DEM < sea level`, not a dynamically coupled storm-tide model.
6. **Depression storage includes above-ground volume** up to the rim
   (shallow pans mostly store water above ground).
7. Inter-depression spill is volume-based to the downstream catchment of rim
   cells (no spill timing).

The animation is **not decorative**: each frontend stage renders the engine's
stage grid (PNG-normalised depth rasters), and asset states at each stage come
from the engine's per-stage exposure array sampled from those same grids.

## 4. Heat model

Air temperature = regional background (seasonal normal + user anomaly) minus
lapse rate (6.5 °C/km × elevation anomaly) plus proximity shifts from land
cover rasterised from OSM: built-up areas heat (UHI proxy, +2.5 °C decay over
~120 m), water cools (−3 °C over ~300 m). Exposure uses a heat index normalised
33–45 °C. Assumptions: no humidity/wind in the index, no diurnal cycle,
land-cover classes limited to what the current extract contains.

## 5. Cyclone model

Wind field: parametric rotating vortex (Holland-style radial profile with
roughness decay over land); peak wind from the Saffir-Simpson category.
Surge: category-scaled sea-level offset inundating low-lying DEM cells.
Exposure = max(wind, surge) with wind de-rated 60% for roads/transport.
Assumptions: no forward motion/asymmetry, no rainfall coupling, no waves,
static landfall location (user-clickable on the map).

## 5b. Landslide model

Deterministic susceptibility index on the DEM grid (no spreading; single
"peak saturation" stage, like heat):

* **Slope control**: `slope` (from the filled DEM) converts to degrees;
  susceptibility is 0 below a configurable threshold (default 15°) and
  rises linearly to 1 at the critical angle (default 35°). The infinite-
  slope stability picture, not a mechanistic model.
* **Rainfall / soil-moisture proxy**: with no pore-pressure data, the
  triggering stress is proxied by scenario rainfall — factor 0.5 (dry,
  0 mm) to 1.0 (fully saturating, ≥ 600 mm/24 h) multiplies the slope term.
  This is the standard rainfall-threshold simplification.
* **Exposure**: susceptibility sampled at the asset (mean over its
  vertices); facility groups de-rated 40% (they fail on access/backup, not
  burial). H = 10 × susceptibility.

Assumptions: no soil/rock type, no vegetation cover, no lithology, no
history of instability, no runout modelling (what is flagged is the
*source* zone; debris travel distance is not simulated).

## 6. Vulnerability, criticality, population

- Class tables in `risk/criticality.py` derive criticality/vulnerability from
  OSM tags only (no hand-tuned per-asset data).
- `POP_PER_CAPITA_FACTOR = 2200` residents served per critical facility is an
  order-of-magnitude figure for Mumbai (≈ 3 m residents / ≈ 1.4k facilities).
  Population is attributed **only to facility groups** (health/education,
  energy, communication, transport, water) — road segments do not independently
  "serve" population, otherwise every resident is counted once per street.

## 7. Road network analysis

Graph from shared road-segment endpoints (vertex snapping at 1e-6 °); travel
time from haversine length ÷ class speed. Failure threshold `usability ≥ 0.35`
where usability = max(asset risk, exposure·0.9) — passability is physical, not
consequence-weighted. Sub-threshold damage inflates travel time
`× (1 + 2·risk²)`. Detour impact is bounded to the 400 most critical failed
edges; accessibility uses **multi-source Dijkstra** from all hospital nodes
(2 runs, not per-demand runs) for demand points sampled from residential
areas. Isolated demand counts as +45 min penalty.

## 8. Known limitations

- 30 m DEM cannot resolve individual buildings or kerbs; depth at an asset is
  the cell value at its (densified) geometry.
- Dependency graph is proximity-derived (spatial_proximity edges) — no
  utility-feeder topology is published in OSM; edge weights are distance-based.
- Single-threaded synchronous simulation (~10–15 s per run for 11.3k assets).
- PostGIS path is kept but optional; the dashboard runs from the file store.

## 9. How to reproduce

```bash
python scripts/fetch_dem.py          # Copernicus GLO-30 tile → data/raw/dem
python scripts/fetch_osm.py          # OSM layers (incl. residential context)
python scripts/process_osm.py        # clean GeoJSON layers
uvicorn infrashield.api:app --app-dir src   # http://127.0.0.1:8000
pytest tests/                        # 57 offline tests
```
