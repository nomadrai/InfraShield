#!/usr/bin/env python3
"""Initialize the PostgreSQL + PostGIS schema for InfraShield.

Two steps:
1. Create the database if it does not exist (connects to 'postgres' db).
2. Apply sql/001_schema.sql.

Reads PG* settings from .env / environment. Requires a running PostGIS
server; see README for a one-line Docker command.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --createdb infrashield
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infrashield.config import get_settings  # noqa: E402
from infrashield.db.loader import get_engine, init_schema  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logger = logging.getLogger("init_db")

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"


def create_database(target_db: str) -> bool:
    """CREATE DATABASE if missing; connects to the 'postgres' maintenance db."""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    s = get_settings()
    conn = psycopg2.connect(
        host=s["PGHOST"], port=s["PGPORT"], user=s["PGUSER"],
        password=s["PGPASSWORD"], dbname="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
        if cur.fetchone():
            logger.info("Database %s already exists", target_db)
            conn.close()
            return False
        cur.execute(f'CREATE DATABASE "{target_db}"')
        logger.info("Created database %s", target_db)
    conn.close()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--createdb", metavar="NAME", help="create the database first (default: PGDATABASE)")
    parser.add_argument("--schema", type=Path, default=SCHEMA_SQL, help="path to schema SQL")
    args = parser.parse_args(argv)

    settings = get_settings()
    target_db = args.createdb or settings["PGDATABASE"]
    try:
        create_database(target_db)
    except Exception as exc:
        logger.error("Could not create database %s: %s", target_db, exc)
        logger.error("Is PostgreSQL running? See README for the Docker quickstart.")
        return 1

    try:
        init_schema(args.schema)
    except Exception as exc:
        logger.error("Schema init failed: %s", exc)
        return 1

    # Smoke-test PostGIS availability.
    with get_engine().connect() as conn:
        version = conn.exec_driver_sql("SELECT postgis_version()").scalar()
    logger.info("PostGIS ready: %s", version)
    print(f"\nInfraShield schema initialized in database '{target_db}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
