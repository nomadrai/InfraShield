# InfraShield workflow shortcuts
PYTHON ?= python3

.PHONY: help setup venv fetch process load-db init-db test clean

help:
	@echo "InfraShield targets:"
	@echo "  setup      - create .venv and install requirements"
	@echo "  venv       - activate hint"
	@echo "  fetch      - fetch Mumbai OSM data via Overpass"
	@echo "  process    - raw JSON -> cleaned GeoJSON layers"
	@echo "  init-db    - create DB + apply PostGIS schema"
	@echo "  load-db    - load processed layers into PostGIS"
	@echo "  test       - run test suite"

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	@echo "Done. Activate with: source .venv/bin/activate"

venv:
	@echo "source .venv/bin/activate"

fetch:
	$(PYTHON) scripts/fetch_osm.py

process:
	$(PYTHON) scripts/process_osm.py

init-db:
	$(PYTHON) scripts/init_db.py

load-db:
	$(PYTHON) scripts/load_to_postgis.py

test:
	$(PYTHON) -m pytest

clean:
	rm -rf data/cache/overpass/*.json data/processed/geojson/*.geojson
	@echo "Cleared cached responses and processed GeoJSON (raw data kept)."
