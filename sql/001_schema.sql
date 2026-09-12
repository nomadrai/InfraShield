-- InfraShield: PostgreSQL + PostGIS schema (v0.1, foundational).
--
-- Design goals:
--   * infra_asset holds every OSM-derived feature, point or line, tagged with
--     group/class so hazard scoring and graph building stay uniform.
--   * infra_dependency is the graph edge table: "source depends on target"
--     means "if target fails, source is (partially) impacted".
--   * hazard_* tables are placeholders for the future risk engine; they give
--     the schema its final shape now so ingestion code does not migrate twice.

CREATE EXTENSION IF NOT EXISTS postgis;

------------------------------------------------------------------
-- Study area (the Mumbai 10x10 km AOI used for all extracts)
------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS study_area (
    id          serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    bbox        geometry(Polygon, 4326) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

------------------------------------------------------------------
-- Infrastructure assets (roads, bridges, hospitals, power, ...)
------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra_asset (
    id          bigserial PRIMARY KEY,
    osm_type    text NOT NULL,                -- node | way | relation
    osm_id      bigint NOT NULL,
    category    text NOT NULL,                -- registry name (categories.yaml)
    group_name  text NOT NULL,                -- roads | transport | energy | ...
    class       text,                         -- finer OSM class, e.g. 'primary', 'hospital'
    name        text,
    tags        jsonb NOT NULL DEFAULT '{}'::jsonb,
    length_km   double precision,
    geom        geometry(Geometry, 4326) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_infra_asset_geom      ON infra_asset USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_infra_asset_category  ON infra_asset (category);
CREATE INDEX IF NOT EXISTS idx_infra_asset_group     ON infra_asset (group_name);
CREATE INDEX IF NOT EXISTS idx_infra_asset_tags      ON infra_asset USING gin (tags);

------------------------------------------------------------------
-- Dependency / interdependency graph between assets
------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra_dependency (
    id          bigserial PRIMARY KEY,
    source_id   bigint NOT NULL REFERENCES infra_asset(id) ON DELETE CASCADE,
    target_id   bigint NOT NULL REFERENCES infra_asset(id) ON DELETE CASCADE,
    dep_type    text NOT NULL DEFAULT 'spatial_proximity',  -- future: serves_area, power_feed...
    weight      double precision NOT NULL DEFAULT 1.0,
    metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, target_id, dep_type),
    CHECK (source_id <> target_id)
);

CREATE INDEX IF NOT EXISTS idx_infra_dependency_source ON infra_dependency (source_id);
CREATE INDEX IF NOT EXISTS idx_infra_dependency_target ON infra_dependency (target_id);

------------------------------------------------------------------
-- Hazard / risk engine (schema only -- engine itself comes later)
------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hazard_event (
    id          bigserial PRIMARY KEY,
    hazard_type text NOT NULL,               -- flood | landslide | heat | cyclone
    name        text,
    severity    double precision,            -- 0..1 normalized
    occurred_at timestamptz,
    geom        geometry(Geometry, 4326),
    attributes  jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_hazard_event_geom ON hazard_event USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_hazard_event_type ON hazard_event (hazard_type);

CREATE TABLE IF NOT EXISTS asset_risk_score (
    id          bigserial PRIMARY KEY,
    asset_id    bigint NOT NULL REFERENCES infra_asset(id) ON DELETE CASCADE,
    hazard_type text NOT NULL,
    score       double precision NOT NULL,   -- 0..1
    components  jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {exposure, sensitivity, adaptive_capacity}
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asset_id, hazard_type)
);

CREATE INDEX IF NOT EXISTS idx_asset_risk_score_asset ON asset_risk_score (asset_id);

------------------------------------------------------------------
-- Processed layer catalog (what the pipeline has produced)
------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processing_run (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    bbox        text,
    note        text
);

-- Simple provenance: which raw file fed which layer.
CREATE TABLE IF NOT EXISTS layer_provenance (
    id          bigserial PRIMARY KEY,
    run_id      bigint REFERENCES processing_run(id) ON DELETE CASCADE,
    layer       text NOT NULL,
    raw_path    text,
    features    integer,
    crs         text NOT NULL DEFAULT 'EPSG:4326',
    created_at  timestamptz NOT NULL DEFAULT now()
);
