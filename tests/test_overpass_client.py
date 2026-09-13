"""Overpass client tests (no network: query rendering + cache behavior)."""

from __future__ import annotations

import json
from pathlib import Path

from infrashield.config import load_study_area
from infrashield.osm.client import OverpassClient


def _client(tmp_path: Path) -> OverpassClient:
    return OverpassClient(
        study_area=load_study_area(),
        cache_dir=tmp_path / "cache",
        raw_dir=tmp_path / "raw",
        retries=1,
        sleep_between_requests=0,
    )


def test_render_query_wraps_body_and_bbox(tmp_path: Path) -> None:
    client = _client(tmp_path)
    area = client.study_area
    # NB: Overpass QL has no `#` comments (only //), so no comment kwarg
    rendered = client.render_query('way["highway"="primary"];')

    assert rendered.startswith(f"[out:json][timeout:{client.timeout}][bbox:{area.bbox_overpass}];")
    assert 'way["highway"="primary"];' in rendered
    # statements wrapped in a union block so one `out` emits all results
    assert "(\n" in rendered and "\n);" in rendered
    assert rendered.strip().endswith("out body geom;")


def test_render_query_substitutes_bbox_placeholder(tmp_path: Path) -> None:
    client = _client(tmp_path)
    rendered = client.render_query("node(around:100,{{bbox}});")
    assert "{{bbox}}" not in rendered
    assert "19.085,72.855" in rendered


def test_fetch_query_caches_payload(tmp_path: Path, overpass_payload: dict) -> None:
    client = _client(tmp_path)

    cache_file = client.cache_dir / "cached_cat.json"
    client.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(overpass_payload), encoding="utf-8")

    payload, from_cache = client.fetch_query("unused", cache_key="cached_cat")
    assert from_cache is True
    assert payload["elements"][0]["id"] == 1001


def test_fetch_query_without_cache_key_returns_uncached(tmp_path: Path) -> None:
    client = _client(tmp_path)
    # No cache key and no network: would normally POST. We only assert the
    # cache-key contract here by mocking the post method.
    calls: list[str] = []

    def fake_post(query_text: str) -> dict:
        calls.append(query_text)
        return {"elements": []}

    client._post_with_retries = fake_post  # type: ignore[method-assign]
    payload, from_cache = client.fetch_query("[out:json];")
    assert from_cache is False
    assert payload == {"elements": []}
    assert calls == ["[out:json];"]
