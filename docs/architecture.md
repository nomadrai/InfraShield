# InfraShield Architecture Notes

Short companion to PROJECT.md -- implementation-level details for developers.

## Repository layout

```
infrashield/
├── config/
│   ├── study_area.ini        # Mumbai 10x10 km AOI (single source of truth)
│   ├── categories.yaml       # infrastructure category registry -> query files
│   └── overpass/             # one Overpass .ql file per category
├── src/infrashield/          # importable package (src layout)
│   ├── config.py             # study area, registry, settings (.env loader)
│   ├── osm/client.py         # Overpass client (render, POST, cache, retries)
│   ├── geo/                  # overpass_io (JSON -> GeoDataFrame), processing
│   ├── db/loader.py          # SQLAlchemy/GeoAlchemy2 PostGIS loading
│   └── graph/dependencies.py # networkx dependency graph + cascade
├── scripts/                  # runnable pipeline CLIs (fetch/process/db/load)
├── sql/001_schema.sql        # PostGIS DDL (assets, dependencies, hazards)
├── data/                     # raw/ processed/ cache/ (gitignored)
├── tests/                    # pytest suite (offline, fixture-based)
└── docker-compose.yml        # local PostGIS 16 + PostGIS 3.4
```

## Data flow

```
Overpass API ──fetch_osm.py──▶ data/raw/osm/<category>.json
        │                            │
        │                   process_osm.py
        │                    ├─ elements_to_geodataframe()  (geom-aware)
        │                    ├─ clean_geometries()          (make_valid)
        │                    ├─ clip_to_study_area()        (bbox overlay)
        │                    └─ add_length_km()             (UTM 43N)
        │                            │
        └──▶ data/cache/overpass/    ▼
             (byte cache)     data/processed/geojson/<layer>.geojson
                                     │
                        load_to_postgis.py / db.loader
                                      ▼
                        PostgreSQL + PostGIS (sql/001_schema.sql)
                                      ▼
                        graph.dependencies (networkx)  →  hazard engine (later)
```

## Key conventions

* **CRS**: store WGS84 (EPSG:4326); compute in UTM 43N (EPSG:32643).
* **Category registry**: `config/categories.yaml` is the single source of
  truth; adding a category = adding one `.ql` file + one registry entry.
* **Tags storage**: processed layers keep OSM tags as a JSON string column;
  the PostGIS schema uses `jsonb` with a GIN index for tag queries.
* **Graph direction**: edge `A -> B` = "A depends on B" = "B failing impacts A".
* **Caching**: byte-cache per category under `data/cache/overpass/`; raw
  canonical responses under `data/raw/osm/`. `--no-cache` forces re-download.

## Extending

* **New category**: add `config/overpass/<group>/<name>.ql` + registry entry.
* **New dependency type**: extend `infra_dependency.dep_type`; the graph API
  already accepts explicit curated edges.
* **Hazard engine**: implement against `hazard_event` + `asset_risk_score`
  tables; ingestion never needs to migrate again.
