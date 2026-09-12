"""Central InfraShield configuration.

Loads the study-area definition and the Overpass category registry
(``config/categories.yaml``) and exposes them as simple dataclasses.

All paths are resolved relative to the project root (the directory that
contains ``src/``), so scripts work regardless of the caller's cwd.
"""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# src/infrashield/config.py -> project root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"


@dataclass(frozen=True)
class StudyArea:
    """The 10 x 10 km Mumbai bounding box."""

    south: float
    west: float
    north: float
    east: float

    @property
    def bbox_overpass(self) -> str:
        """Bounding box in Overpass QL order: south, west, north, east."""
        return f"{self.south},{self.west},{self.north},{self.east}"

    def as_dict(self) -> dict[str, float]:
        return {
            "south": self.south,
            "west": self.west,
            "north": self.north,
            "east": self.east,
        }


def load_study_area(path: Path | None = None) -> StudyArea:
    """Load the study area from config/study_area.ini."""
    ini_path = path or (CONFIG_DIR / "study_area.ini")
    parser = ConfigParser()
    read = parser.read(ini_path)
    if not read:
        raise FileNotFoundError(f"Study area config not found: {ini_path}")

    bbox = parser["bounding_box"]
    return StudyArea(
        south=bbox.getfloat("south"),
        west=bbox.getfloat("west"),
        north=bbox.getfloat("north"),
        east=bbox.getfloat("east"),
    )


@dataclass(frozen=True)
class Category:
    """One OSM infrastructure category, tied to an Overpass query file."""

    name: str
    layer: str
    group: str
    description: str
    query: Path

    @property
    def query_name(self) -> str:
        return self.query.stem


def load_categories(path: Path | None = None) -> list[Category]:
    """Load the category registry from config/categories.yaml."""
    yaml_path = path or (CONFIG_DIR / "categories.yaml")
    if not yaml_path.exists():
        raise FileNotFoundError(f"Category registry not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as fh:
        entries = yaml.safe_load(fh) or []

    categories: list[Category] = []
    for entry in entries:
        categories.append(
            Category(
                name=entry["name"],
                layer=entry["layer"],
                group=entry["group"],
                description=entry.get("description", ""),
                query=(CONFIG_DIR / "overpass" / entry["query"]).resolve(),
            )
        )
    return categories


def get_category(name: str) -> Category:
    """Look up a single category by name (ValueError if unknown)."""
    for category in load_categories():
        if category.name == name:
            return category
    known = ", ".join(c.name for c in load_categories())
    raise ValueError(f"Unknown category {name!r}. Known categories: {known}")


@lru_cache(maxsize=1)
def get_settings() -> dict[str, str]:
    """Optional environment overrides from .env / process environment.

    Only reads keys it knows; everything else in the env is ignored.
    """
    env_file = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")

    defaults = {
        "OVERPASS_ENDPOINT": "https://overpass-api.de/api/interpreter",
        "OVERPASS_TIMEOUT": "180",
        "OVERPASS_RETRIES": "3",
        "OVERPASS_CACHE_DIR": str(DATA_DIR / "cache" / "overpass"),
        "PGHOST": "localhost",
        "PGPORT": "5432",
        "PGDATABASE": "infrashield",
        "PGUSER": "postgres",
        "PGPASSWORD": "",
    }
    defaults.update(values)
    return defaults
