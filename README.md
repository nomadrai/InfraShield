# InfraShield

**Geospatial decision support for critical infrastructure under environmental stress.**

InfraShield helps authorities understand how floods, landslides, extreme heat, cyclones,
and other hazards affect critical infrastructure — identifying vulnerable assets, estimating
risk severity, and modeling **cascading effects** across interconnected systems
(e.g., *heavy rainfall → bridge inaccessible → road network disrupted → hospital
accessibility drops → communities affected*).

Primary study area: a **10 × 10 km region of Mumbai, India**, built on OpenStreetMap data
via the Overpass API.

> Full background, planned architecture, and roadmap: see [PROJECT.md](PROJECT.md).
> Implementation notes for developers: [docs/architecture.md](docs/architecture.md).

## Project structure

```
infrashield/
├── config/
│   ├── study_area.ini        # Mumbai 10x10 km bounding box (single source of truth)
│   ├── categories.yaml       # infrastructure category registry
│   └── overpass/             # one Overpass .ql query per category
├── src/infrashield/          # core package (config, osm, geo, db, graph)
├── scripts/                  # fetch_osm.py, process_osm.py, init_db.py, load_to_postgis.py
├── sql/001_schema.sql        # PostgreSQL + PostGIS schema
├── data/                     # raw/ processed/ cache/ (gitignored, dirs kept via .gitkeep)
├── tests/                    # pytest suite
├── docs/architecture.md      # developer architecture notes
├── docker-compose.yml        # local PostGIS
├── .env.example              # environment template
└── Makefile                  # common workflow shortcuts
```

## Quickstart

Requires **Python 3.10+**.

```bash
# 1. Environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (optional — sane defaults are built in)
cp .env.example .env

# 3. Fetch Mumbai OSM data via Overpass (~10 categories, polite rate limiting)
python scripts/fetch_osm.py

# 4. Process into clean GeoJSON layers
python scripts/process_osm.py
```

Results land in `data/raw/osm/` (raw JSON) and `data/processed/geojson/` (layers),
with summaries in `data/processed/processing_summary.csv`.

### Useful variations

```bash
python scripts/fetch_osm.py --list              # show all categories
python scripts/fetch_osm.py hospitals bridges   # fetch a subset
python scripts/fetch_osm.py --no-cache          # ignore cached responses
python scripts/process_osm.py hospitals         # process one category
```

Or via make: `make setup`, `make fetch`, `make process`.

## Simulation dashboard (Phase 2)

The full risk engine + interactive dashboard now run on the real data — DEM-driven
flood propagation, heat/cyclone fields, road-network impact, dependency cascades.
See **[docs/algorithm.md](docs/algorithm.md)** for the algorithm comparison decision,
the pipeline, and every documented simplification.

```bash
# one-time: fetch the real Copernicus GLO-30 DEM for the AOI
python scripts/fetch_dem.py

# optional: CARTO basemap key (removes the "API key required" watermark)
cp .env.example .env       # then set CARTO_BASEMAP_KEY=... from carto.com/basemap/apikey

# start the API + dashboard (serves web/ at /)
uvicorn infrashield.api:app --app-dir src
# open http://127.0.0.1:8000
```

Without a basemap key the dashboard falls back to OpenFreeMap (free, no watermark).
Quota-wise, one map view loads ~28 tiles and a heavy interactive session ~1–2k, so
the free tier (5M tiles/month) comfortably covers hundreds of sessions.

The dashboard lets you pick hazard (flood / extreme heat / cyclone), severity and —
for cyclones — the landfall point on the map, then replays the actual simulated
stages: water spreading through depressions and channels, infrastructure turning
amber/red as the hazard reaches it, cascade edges lighting up, and per-asset
risk vs impact inspection (including per-stage exposure charts and dependency
chains). Every number shown is computed by the engine from the DEM + OSM data;
nothing is scripted.

Run the whole pipeline headlessly:

```python
from infrashield.store import load_terrain, load_layers, build_dependency_graph
from infrashield.risk.scenario import run_scenario

result = run_scenario(
    load_terrain(), load_layers(), build_dependency_graph(load_layers()),
    "flood", {"rainfall_mm": 300, "duration_h": 24},
)
print(result.summary)
```

## PostgreSQL + PostGIS (optional for core pipeline)

```bash
docker compose up -d                       # PostGIS 16 + PostGIS 3.4 on :5432
python scripts/init_db.py                  # create DB + apply sql/001_schema.sql
python scripts/load_to_postgis.py          # load processed GeoJSON layers
```

The schema (`sql/001_schema.sql`) defines `infra_asset`, `infra_dependency`
(the graph edge table), `hazard_event`, and `asset_risk_score` — the last two
are ready for the future hazard/risk engine.

## The dependency graph

```python
from infrashield.graph import build_proximity_graph, cascade_from
import geopandas as gpd

assets = gpd.read_file("data/processed/geojson/hospitals.geojson")
# ...combine layers, ensure a group_name column, then:
graph = build_proximity_graph(assets)
impacts = cascade_from(graph, "node/1234567890")   # ranked downstream impacts
```

Edges mean *"A depends on B"* — if B fails, A is impacted. Phase 1 derives edges
from spatial proximity; curated edges (power feeds, water mains, service areas)
plug into the same schema later.

## Overpass queries

Each category lives in `config/overpass/<group>/<name>.ql` — a plain Overpass QL
body (registered in `config/categories.yaml`). The client wraps it with
`[out:json][timeout:…][bbox:…]` + `out body geom;` and substitutes `{{bbox}}`
with the study area. To add a category: create a `.ql` file, add a registry
entry, re-run fetch + process.

Queries are POSTed to `https://overpass-api.de/api/interpreter` with retries
(429/504 backoff) and a local byte cache, so repeat runs don't hammer the
public endpoint. Please keep it that way — see the
[Overpass API usage policy](https://operations.osmfoundation.org/policies/overpass/).

## Study area

`config/study_area.ini` defines the Mumbai AOI: bbox `19.085, 72.855 → 19.175, 72.965`
(centered near Powai / IIT Bombay, ≈ 10.5 × 11 km). All fetching, clipping, and
clipping derive from this single file. CRS conventions: store **WGS84 (EPSG:4326)**,
compute in **UTM 43N (EPSG:32643)**.

## Testing

```bash
pytest            # offline unit tests (fixture-based, no network)
```

## Current status

- **Phase 1 (done):** acquisition → processing → storage pipeline for the Mumbai AOI.
- **Phase 2 (done):** hazard engine (topographic flood, heat, cyclone), exposure →
  risk → network → cascade pipeline, FastAPI backend, interactive dashboard,
  57 offline tests. Algorithm decisions and assumptions: [docs/algorithm.md](docs/algorithm.md).
- **Next:** calibrated hydrology (gauge/radar forcing), curated dependency edges
  (utility feeders), async job queue for long scenarios, PostGIS-backed store.

See [PROJECT.md](PROJECT.md) for the original roadmap.

## License

MIT (placeholder — adjust as needed).
