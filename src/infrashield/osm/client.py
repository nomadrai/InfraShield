"""Minimal Overpass API client with caching, retries, and rate limiting.

The client renders each ``.ql`` query body into a full Overpass QL document:

    [out:json][timeout:180];
    // category comment
    <query body>;
    out body geom;

``{{bbox}}`` inside a query body is replaced with the study-area bbox, and
the ``[bbox:...]`` setting scopes every statement to the AOI as a safety net.

Raw responses are stored under ``data/raw/osm/<category>.json``; a byte-cache
under ``data/cache/overpass/`` avoids re-downloading on repeated runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from infrashield.config import (
    RAW_DATA_DIR,
    Category,
    StudyArea,
    get_settings,
    load_study_area,
)

logger = logging.getLogger(__name__)

OVERPASS_TIMEOUT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


@dataclass
class FetchResult:
    """Outcome of a single category fetch."""

    category: str
    endpoint: str
    element_count: int
    raw_path: Path
    from_cache: bool
    elapsed_s: float


class OverpassClient:
    """Small, explicit Overpass API client (no external q/OSMnx dependency)."""

    def __init__(
        self,
        study_area: StudyArea | None = None,
        endpoint: str | None = None,
        timeout: int | None = None,
        retries: int | None = None,
        cache_dir: Path | None = None,
        raw_dir: Path | None = None,
        sleep_between_requests: float = 2.0,
    ) -> None:
        settings = get_settings()
        self.study_area = study_area or load_study_area()
        self.endpoint = endpoint or settings["OVERPASS_ENDPOINT"]
        self.timeout = int(timeout or settings["OVERPASS_TIMEOUT"])
        self.retries = int(retries if retries is not None else settings["OVERPASS_RETRIES"])
        self.cache_dir = Path(cache_dir or settings["OVERPASS_CACHE_DIR"])
        self.raw_dir = Path(raw_dir or (RAW_DATA_DIR / "osm"))
        self.sleep_between_requests = sleep_between_requests
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "InfraShield/0.1 (infrastructure risk research)"

    # ------------------------------------------------------------------ #
    # Query rendering
    # ------------------------------------------------------------------ #

    def render_query(self, body: str) -> str:
        """Render a .ql body into a complete Overpass QL document.

        The body's statements are wrapped in a union block ``( ... );`` so a
        single ``out`` emits *every* statement's result -- without the union,
        Overpass would only output the last statement's default set.
        """
        bbox = self.study_area.bbox_overpass
        body = body.replace("{{bbox}}", bbox)
        header = f"[out:json][timeout:{self.timeout}][bbox:{bbox}];"
        indented = "\n".join(
            f"  {line}" if line.strip() else line for line in body.strip().splitlines()
        )
        return f"{header}\n(\n{indented}\n);\nout body geom;\n"

    # ------------------------------------------------------------------ #
    # Fetching
    # ------------------------------------------------------------------ #

    def fetch_query(self, query_text: str, cache_key: str | None = None) -> tuple[dict, bool]:
        """POST a rendered query; return (payload, from_cache).

        Results are cached on disk (one file per cache key) so repeated runs
        and offline processing do not hammer the public endpoint.
        """
        cache_path = None
        if cache_key:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = self.cache_dir / f"{cache_key}.json"
            if cache_path.exists():
                logger.info("Cache hit for %s (%s)", cache_key, cache_path)
                return json.loads(cache_path.read_text(encoding="utf-8")), True

        payload = self._post_with_retries(query_text)

        if cache_path is not None:
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload, False

    def fetch_category(self, category: Category) -> FetchResult:
        """Fetch one infrastructure category defined in categories.yaml."""
        if not category.query.exists():
            raise FileNotFoundError(f"Query file missing: {category.query}")

        body = category.query.read_text(encoding="utf-8")
        rendered = self.render_query(body)

        started = time.monotonic()
        payload, from_cache = self.fetch_query(rendered, cache_key=category.name)
        elapsed = time.monotonic() - started

        self.raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self.raw_dir / f"{category.name}.json"
        raw_path.write_text(json.dumps(payload), encoding="utf-8")

        elements = payload.get("elements", [])
        logger.info(
            "Fetched %s: %d elements in %.1fs%s",
            category.name, len(elements), elapsed, " (cache)" if from_cache else "",
        )
        return FetchResult(
            category=category.name,
            endpoint=self.endpoint,
            element_count=len(elements),
            raw_path=raw_path,
            from_cache=from_cache,
            elapsed_s=elapsed,
        )

    def _post_with_retries(self, query_text: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._session.post(
                    self.endpoint,
                    data={"data": query_text},
                    timeout=self.timeout + 30,
                )
                response.raise_for_status()
                return response.json()
            except OVERPASS_TIMEOUT_ERRORS as exc:  # transient network issues
                last_error = exc
                wait = min(60, 5 * 2**attempt)
                logger.warning(
                    "Overpass request failed (attempt %d/%d): %s -- retrying in %ds",
                    attempt, self.retries, exc, wait,
                )
                time.sleep(wait)
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (429, 504):  # rate limited / gateway timeout
                    last_error = exc
                    wait = min(90, 10 * 2**attempt)
                    logger.warning(
                        "Overpass returned HTTP %s (attempt %d/%d) -- retrying in %ds",
                        status, attempt, self.retries, wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Overpass request failed after {self.retries} attempts") from last_error


def fetch_category(category: Category, **client_kwargs) -> FetchResult:
    """Convenience helper: fetch a single category with default settings."""
    return OverpassClient(**client_kwargs).fetch_category(category)


def fetch_all_categories(
    categories: list[Category], **client_kwargs
) -> list[FetchResult]:
    """Fetch every category sequentially, pausing politely between requests."""
    client = OverpassClient(**client_kwargs)
    results: list[FetchResult] = []
    for index, category in enumerate(categories):
        if index:
            time.sleep(client.sleep_between_requests)
        results.append(client.fetch_category(category))
    return results
