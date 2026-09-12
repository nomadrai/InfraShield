# InfraShield — Project Overview & Plan

## 1. Project overview

InfraShield is a geospatial decision-support system that helps authorities understand how
environmental events — **floods, landslides, extreme heat, cyclones, and other hazards** —
affect critical infrastructure. It identifies vulnerable assets, estimates risk severity,
models **cascading effects across interconnected infrastructure**, and prioritizes where
preventive action or preparedness is needed.

Roads, bridges, power lines, hospitals, and other essential facilities can be damaged or
become hard to access during environmental events. Cities usually *know* where their assets
are, but lack a clear picture of which ones are exposed as conditions change, how severe the
risk is, and what wider impacts would follow. InfraShield closes that gap by combining
infrastructure, terrain, weather, hazard, Earth Observation, population, and
network/dependency data.

**Canonical example of the problem class:**

> Heavy rainfall → bridge becomes inaccessible → road network is disrupted →
> hospital accessibility decreases → surrounding communities are affected.

Primary region: **Mumbai, India** — approximately **10 km × 10 km**.

## 2. Problem statement

When a hazard event strikes (or is forecast), city authorities need answers to:

1. **Which critical assets are exposed?** Roads, bridges, hospitals, schools, rail, power
   lines, substations, communication towers, waterways intersected by hazard zones.
2. **How severe is the risk per asset?** Combining exposure (hazard intensity at the asset),
   sensitivity (asset type, criticality), and adaptive capacity (redundancy, alternatives).
3. **What cascades?** A flooded bridge doesn't just stop traffic — it cuts routes, lengthens
   hospital trips, isolates neighborhoods. Interdependencies turn local damage into
   network-wide service loss.
4. **What to act on first?** A ranked, evidence-based priority list for prevention and
   preparedness under limited budgets.

## 3. Objectives

| # | Objective | Output |
|---|-----------|--------|
| O1 | Unified infrastructure inventory for the AOI | Clean geospatial layers (roads, bridges, hospitals, schools, rail, power, telecom, water) |
| O2 | Hazard exposure assessment | Per-asset, per-hazard exposure scores |
| O3 | Vulnerability & risk ranking | 0–1 risk scores with explainable components |
| O4 | Cascading-impact analysis | Dependency graph + impact propagation from any failed asset |
| O5 | Prioritization support | Ranked list of assets for intervention, with rationale |
| O6 | Reproducible pipeline | One-command fetch → process → load → analyze |

## 4. Initial scope — Mumbai 10 × 10 km

* **AOI**: bounding box `19.085, 72.855 → 19.175, 72.965` (WGS84) — centered near
  Powai / IIT Bombay, covering parts of Andheri, Powai, Vikhroli, Ghatkopar, and Marol.
  ≈ 10.5 km N–S × 11 km E–W (kept on round decimal degrees; effectively the requested 10 × 10 km).
* **Primary data source**: OpenStreetMap via Overpass API
  (`https://overpass-api.de/api/interpreter`).
* **Why Mumbai**: dense critical infrastructure, monsoon flooding and landslides, rich OSM
  coverage, strong public-interest use case.
* **Configured asset categories** (`config/overpass/`): major/minor roads, bridges, railways
  (tracks + stations), airports, power (lines, substations, plants, towers), telecom masts,
  waterways/water bodies, hospitals & emergency services, schools & universities.
* Later phases widen the region; the pipeline is bbox-driven from
  `config/study_area.ini`, so widening is a config edit.

## 5. Planned architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          DATA LAYER                                 │
│  OSM/Overpass   DEM (terrain)   Weather/rainfall   Hazard maps      │
│  Earth Observation   Population (WorldPop/Census)   Utility records │
└──────────────┬──────────────────────────────────────────────────────┘
               │  fetchers (per source, cache + raw store: data/raw/)
┌──────────────▼──────────────────────────────────────────────────────┐
│                     PROCESSING LAYER                                │
│  parsing → validation → cleaning → clipping → CRS normalization     │
│  (GeoPandas/Shapely; outputs: data/processed/ + PostGIS)            │
└──────────────┬──────────────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────────────┐
│                      STORAGE LAYER                                  │
│  PostgreSQL + PostGIS: infra_asset · infra_dependency · hazard_event│
│  · asset_risk_score · provenance tables                             │
└──────────────┬──────────────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────────────┐
│                      ANALYSIS LAYER                                 │
│  Infrastructure graph (networkx) → hazard exposure → vulnerability  │
│  → cascading impact (weighted propagation) → prioritization         │
└──────────────┬──────────────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────────────┐
│                      DELIVERY LAYER                                 │
│  Reports / dashboards / map views / API (later phases)              │
└─────────────────────────────────────────────────────────────────────┘
```

## 6. Data sources

| Source | Provides | Access | Phase |
|--------|----------|--------|-------|
| OpenStreetMap / Overpass | roads, bridges, hospitals, schools, rail, power, telecom, waterways | HTTP (implemented) | 1 ✅ |
| DEM (e.g., Copernicus GLO-30, SRTM) | elevation, slope, flow accumulation | HTTP download | 2 |
| IMD / Open-Meteo / NASA POWER | rainfall, temperature extremes | HTTP APIs | 2–3 |
| Flood maps (Fathom, JBA, municipal) / EO flood extent (Sentinel-1) | flood-prone areas, observed inundation | download / GEE | 3 |
| Landslide susceptibility layers | landslide-prone slopes | research datasets | 3 |
| Heat indices (LST from MODIS/Landsat, heatwave records) | extreme-heat exposure | GEE / download | 3 |
| WorldPop / Census of India | population density, exposure of communities | download | 3 |
| Municipal utility records | curated dependencies (power feeds, water mains) | manual/partnership | 4 |

## 7. OSM / Overpass methodology

1. **Study area as config** — `config/study_area.ini` holds the bbox; every query is scoped
   with `[bbox:…]` and `{{bbox}}` substitution.
2. **One query file per category** — `config/overpass/<group>/<name>.ql`, registered in
   `config/categories.yaml` (name, layer, group, description, query path).
3. **Rendered QL** — the client wraps each body with `[out:json][timeout:N][bbox:…]`
   and appends `out body geom;`, so every element carries explicit geometry (no node
   reference resolution needed client-side).
4. **Polite fetching** — POST to the interpreter endpoint, retries with exponential backoff
   on 429/504 and network errors, sequential requests with a delay, custom User-Agent.
5. **Raw store + byte cache** — canonical responses in `data/raw/osm/<category>.json`,
   byte cache in `data/cache/overpass/` so re-runs are free and offline-safe.
6. **Conversion** — Overpass JSON → GeoDataFrame (`elements_to_geodataframe`), OSM tags kept
   as a JSON column; per-layer geometry typing; de-duplication by `(osm_type, osm_id)`.
7. **Cleaning & clipping** — invalid geometries made valid, everything clipped to the AOI,
   line layers get metric `length_km` (UTM 43N).
8. **Outputs** — GeoJSON per layer + CSV/JSON summaries; optional PostGIS load.

## 8. Planned hazard / risk engine (schema ready — not implemented yet)

Per asset and hazard:

```
risk = exposure × sensitivity × (1 − adaptive_capacity)      (all normalized 0–1)
```

* **Exposure**: hazard intensity at the asset (flood depth from DEM+rainfall or flood maps,
  slope/soil for landslides, LST for heat, wind fields for cyclones).
* **Sensitivity**: asset class (bridge > footway; hospital > clinic), structural attributes,
  criticality ranking.
* **Adaptive capacity**: redundancy (alternative routes/spares), elevation of critical
  equipment, response capacity nearby.

Outputs land in `asset_risk_score (asset_id, hazard_type, score, components{…})` —
components stored per score so rankings stay explainable and auditable. The schema
(`sql/001_schema.sql`) is already in place; ingestion will not need to migrate.

## 9. Vulnerability assessment (planned)

* **Criticality weighting**: hospitals, fire/police, substations, arterial bridges score
  higher; weights configurable per class.
* **Accessibility analysis**: service-area / shortest-path degradation on the road network
  under hazard scenarios (travel-time increase to nearest hospital).
* **Population coupling**: people affected per degraded asset = population in the losing
  service areas (WorldPop rasters × network analysis).
* **Equity lens**: compare impact across neighborhoods, flag disproportionate burden.

## 10. Cascading-impact analysis (planned)

* **Graph model**: directed edge `A → B` = "A depends on B"; failure of B propagates to A.
* **Edge types**: `spatial_proximity` (phase 1, implemented as a proxy), then curated
  `power_feed`, `water_supply`, `serves_area`, `road_access`.
* **Propagation**: weighted decay along dependency paths (`cascade_from` is implemented);
  later iterations add network-flow realism (road connectivity, power radial networks).
* **Scenario runs**: "bridge X floods" → ranked list of impacted hospitals, schools,
  substations, and estimated affected population.

## 11. Infrastructure graph / dependencies

* Phase 1 (implemented): proximity-based edges from facilities (hospitals/schools) to
  nearby road/energy/communication/water assets, distance-capped and weighted by closeness;
  stored in `infra_dependency` with type + weight + provenance metadata.
* Phase 2: topology-derived edges — power lines to substations, road network connectivity,
  bridge "depends on" road segments.
* Phase 3: curated/utility datasets and service-area dependencies.
* The `infra_dependency` table and the networkx API accept all of these without schema
  changes (`dep_type`, `weight`, `metadata`).

## 12. Technology stack

| Concern | Choice | Status |
|---------|--------|--------|
| Language | Python 3.10+ | ✅ |
| Geospatial processing | GeoPandas, Shapely 2.x | ✅ |
| Data acquisition | requests (Overpass client) | ✅ |
| Storage | PostgreSQL 16 + PostGIS 3.4 (Docker) | ✅ schema ready |
| DB access | SQLAlchemy 2, GeoAlchemy2, psycopg2 | ✅ |
| Graph | networkx 3 | ✅ |
| Tables (tabular) | pandas | ✅ |
| Config | YAML + INI + `.env` | ✅ |
| Testing | pytest | ✅ |
| Later: rasters / EO | rasterio, rasterstats, xarray | planned |
| Later: networks | osmnx or custom; possibly igraph | planned |
| Later: dashboards | Streamlit / GeoNode / web map | planned |
| Later: orchestration | Airflow / Prefect (if needed) | planned |

## 13. Development phases

* **Phase 1 — Foundation (current)** ✅
  Project structure, Overpass client + query library (10 categories), raw/processed data
  stores, GeoJSON processing pipeline, PostGIS schema, proximity dependency graph,
  tests, docs.
* **Phase 2 — Terrain & hazard data**
  DEM acquisition + slope/elevation rasters; rainfall/time-series fetchers; flood and
  landslide susceptibility layers; raster-vector zonal exposure joins.
* **Phase 3 — Risk engine (first version)**
  Exposure × sensitivity scoring per asset/hazard; population exposure; explainable
  `asset_risk_score` output; simple ranked reports.
* **Phase 4 — Cascading & accessibility**
  Road-network graph with travel-time degradation; hospital accessibility under scenarios;
  curated dependency edges; cascading scenario runner.
* **Phase 5 — Delivery**
  Dashboard/map UI, API, scenario comparison, export/prioritization reports.

## 14. Current status

**Phase 1 complete — runnable foundation.**

Implemented and working:

* 📁 Scalable repo layout (src-layout package, config-driven pipeline)
* 🛰️ Overpass client: rendered QL, POST with retries/backoff, byte cache, raw store
* 🗂️ 10 Overpass query files across 6 category groups (roads, bridges, rail,
  airports, power, telecom, waterways, hospitals/emergency, schools)
* 🧹 Geo utilities: Overpass JSON → GeoDataFrame, geometry cleaning (make_valid),
  AOI clipping, metric length computation, per-layer summaries
* 🗄️ PostgreSQL + PostGIS: schema for assets/dependencies/hazards/risk, loader,
  init script, docker-compose
* 🕸️ Dependency graph: proximity build (weighted) + weighted cascade propagation
* ⚙️ Config: study area, category registry, `.env` support, `.gitignore` for data
* 📖 Docs: README, PROJECT.md, architecture notes
* ✅ Offline pytest suite for parsing, processing, and graph logic
* 🧪 End-to-end verified live: ~11,900 OSM elements fetched for the Mumbai AOI,
  processed into 10 GeoJSON layers, and a 11,675-node dependency graph built
  with cascade analysis (single-category runs were also validated)

Not yet implemented (intentionally): hazard/risk engine, DEM/weather ingestion,
cascading scenario runner, dashboards/API.

## 15. Future extensions

* Widen AOI to all of Mumbai / MMR (config change + rescale).
* Real-time ingestion: rainfall nowcast + EO flood extent → live risk refresh.
* Multi-hazard composite scoring (monsoon flood + heat seasonality).
* What-if planning: score *proposed* mitigation (new bridge, elevated substation) by
  re-running cascade scenarios.
* Population-aware prioritization (people-impact per rupee spent).
* Uncertainty communication: score ranges + data-quality flags per asset.
* Integration hooks: export priority lists to municipal GIS (GeoJSON/WFS).

## 16. Data & attribution notes

* OSM data: © OpenStreetMap contributors, ODbL — respect the
  [Overpass usage policy](https://operations.osmfoundation.org/policies/overpass/).
* Population: WorldPop (CC BY 4.0); Census of India as alternative.
* DEM: Copernicus GLO-30 / SRTM public domain or CC-licensed variants.
* This project is a decision-support prototype; outputs are indicative, not operational
  safety advice.
