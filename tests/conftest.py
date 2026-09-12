"""Shared pytest fixtures for InfraShield tests (all offline, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infrashield.config import StudyArea


@pytest.fixture()
def study_area() -> StudyArea:
    return StudyArea(south=19.085, west=72.855, north=19.175, east=72.965)


@pytest.fixture()
def overpass_payload() -> dict:
    """Small Overpass response in the real ``out body geom`` wire format.

    * nodes: top-level lat/lon + tags
    * ways: ``geometry`` as a list of {lat, lon} points (what overpass-api.de
      returns), plus bounds
    """
    return {
        "version": 0.6,
        "elements": [
            {
                "type": "node",
                "id": 1001,
                "lat": 19.12,
                "lon": 72.90,
                "tags": {"amenity": "hospital", "name": "Test Hospital"},
            },
            {
                "type": "node",
                "id": 1002,
                "lat": 19.13,
                "lon": 72.91,
                "tags": {"amenity": "school", "name": "Test School"},
            },
            {
                "type": "way",
                "id": 2001,
                "bounds": {"minlat": 19.10, "minlon": 72.88, "maxlat": 19.115, "maxlon": 72.895},
                "nodes": [1, 2, 3],
                "tags": {"highway": "primary", "bridge": "yes", "name": "Test Bridge Rd"},
                "geometry": [
                    {"lat": 19.10, "lon": 72.88},
                    {"lat": 19.11, "lon": 72.89},
                    {"lat": 19.115, "lon": 72.895},
                ],
            },
            {
                "type": "way",
                "id": 2002,
                "bounds": {"minlat": 19.09, "minlon": 72.86, "maxlat": 19.11, "maxlon": 72.88},
                "nodes": [4, 5, 6],
                "tags": {"waterway": "river", "name": "Test Creek"},
                "geometry": [
                    {"lat": 19.09, "lon": 72.86},
                    {"lat": 19.10, "lon": 72.87},
                    {"lat": 19.11, "lon": 72.88},
                ],
            },
        ],
    }


@pytest.fixture()
def closed_way_payload() -> dict:
    """A closed way (amenity polygon) and a multipolygon-style relation."""
    return {
        "version": 0.6,
        "elements": [
            {
                "type": "way",
                "id": 3001,
                "tags": {"amenity": "hospital", "name": "Poly Hospital", "building": "yes"},
                "geometry": [
                    {"lat": 19.12, "lon": 72.90},
                    {"lat": 19.12, "lon": 72.91},
                    {"lat": 19.13, "lon": 72.91},
                    {"lat": 19.13, "lon": 72.90},
                    {"lat": 19.12, "lon": 72.90},
                ],
            },
            {
                "type": "relation",
                "id": 4001,
                "tags": {"natural": "water", "name": "Test Lake"},
                "members": [
                    {"type": "way", "ref": 5001, "role": "outer",
                     "geometry": [
                         {"lat": 19.10, "lon": 72.86},
                         {"lat": 19.10, "lon": 72.87},
                         {"lat": 19.11, "lon": 72.87},
                         {"lat": 19.11, "lon": 72.86},
                         {"lat": 19.10, "lon": 72.86},
                     ]},
                    {"type": "way", "ref": 5002, "role": "inner",
                     "geometry": [
                         {"lat": 19.105, "lon": 72.865},
                         {"lat": 19.105, "lon": 72.868},
                         {"lat": 19.108, "lon": 72.868},
                         {"lat": 19.108, "lon": 72.865},
                         {"lat": 19.105, "lon": 72.865},
                     ]},
                ],
            },
        ],
    }


@pytest.fixture()
def raw_osm_dir(tmp_path: Path, overpass_payload: dict) -> Path:
    """Temporary data/raw/osm with two raw category files."""
    raw = tmp_path / "raw" / "osm"
    raw.mkdir(parents=True)
    (raw / "hospitals.json").write_text(json.dumps(overpass_payload), encoding="utf-8")
    (raw / "roads.json").write_text(json.dumps(overpass_payload), encoding="utf-8")
    return raw
