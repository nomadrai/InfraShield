"""Config tests: study area, category registry, settings."""

from __future__ import annotations

from pathlib import Path

from infrashield.config import (
    get_category,
    get_settings,
    load_categories,
    load_study_area,
)


def test_load_study_area_from_repo_config() -> None:
    area = load_study_area()
    assert area.south < area.north
    assert area.west < area.east
    # Mumbai-ish coordinates
    assert 18.0 < area.south < 20.0
    assert 72.0 < area.west < 73.5
    assert area.bbox_overpass == f"{area.south},{area.west},{area.north},{area.east}"


def test_load_categories_registry() -> None:
    categories = load_categories()
    names = {c.name for c in categories}
    assert {
        "major_roads", "minor_roads", "bridges", "railways", "airports",
        "power", "telecom", "waterways", "hospitals", "schools",
    } <= names
    for cat in categories:
        assert cat.query.exists(), f"missing query file for {cat.name}"
        assert cat.layer and cat.group


def test_get_category_lookup() -> None:
    cat = get_category("hospitals")
    assert cat.group == "health_education"
    assert cat.query_name == "hospitals"


def test_get_category_unknown_raises() -> None:
    try:
        get_category("does_not_exist")
    except ValueError as exc:
        assert "known categories" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")


def test_settings_defaults() -> None:
    settings = get_settings()
    assert settings["OVERPASS_ENDPOINT"].startswith("https://overpass-api.de")
    assert settings["PGDATABASE"] == "infrashield"
