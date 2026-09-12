"""Load processed GeoDataFrames / GeoJSON into PostgreSQL + PostGIS.

Python-side DB access uses SQLAlchemy + GeoAlchemy types so the loader works
even without the ``ogr2ogr`` binary. The schema itself lives in
``sql/001_schema.sql`` (applied via scripts/init_db.py) and is executed over a
raw DBAPI connection: SQLAlchemy's ``text()`` would misread the ``::jsonb``
casts as bind parameters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
from sqlalchemy import create_engine

from infrashield.config import get_settings

logger = logging.getLogger(__name__)


def _connection_url() -> str:
    s = get_settings()
    return (
        f"postgresql+psycopg2://{s['PGUSER']}:{s['PGPASSWORD']}"
        f"@{s['PGHOST']}:{s['PGPORT']}/{s['PGDATABASE']}"
    )


def get_engine():
    """Create a SQLAlchemy engine from .env / environment settings."""
    try:
        return create_engine(_connection_url(), pool_pre_ping=True)
    except ModuleNotFoundError as exc:  # psycopg2 not installed
        raise RuntimeError(
            "psycopg2 not installed. Run: pip install -r requirements-db.txt"
        ) from exc


def init_schema(sql_path: Path | None = None) -> None:
    """Apply sql/001_schema.sql over a raw DBAPI connection.

    A raw cursor (not SQLAlchemy ``text()``) is used so the full multi-statement
    script -- including ``::jsonb`` casts -- executes verbatim.
    """
    sql_path = sql_path or Path(__file__).resolve().parents[3] / "sql" / "001_schema.sql"
    sql_text = Path(sql_path).read_text(encoding="utf-8")
    engine = get_engine()
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(sql_text)
        raw.commit()
    finally:
        raw.close()
    logger.info("Applied schema from %s", sql_path)


def load_layer(
    gdf: gpd.GeoDataFrame,
    layer: str,
    group: str,
    if_exists: str = "replace",
) -> int:
    """Write one processed layer to PostGIS as table ``<layer>``.

    Returns the number of rows written. Rows carry the registry category
    (``layer``) and coarse ``group`` so they can be unified into infra_asset
    later (or queried directly per layer for now).
    """
    if gdf.empty:
        logger.warning("Layer %s is empty -- nothing to load", layer)
        return 0

    gdf = gdf.copy()
    gdf["category"] = layer
    gdf["group_name"] = group

    engine = get_engine()
    gdf.to_postgis(layer, engine, schema="public", if_exists=if_exists, index=False)
    logger.info("Loaded %d features into %s", len(gdf), layer)
    return int(len(gdf))
