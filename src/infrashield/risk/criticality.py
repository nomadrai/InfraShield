"""Per-asset characteristics: criticality, vulnerability, sensitivity.

All values derive from the asset's OSM group/class/tags — no hand-tuned
per-asset data. Scales are 0..1. These parameters drive:

* ``risk = exposure x vulnerability x criticality``  (asset failure risk)
* network-impact weighting (how many services an asset supports)
* cascade decay (how strongly dependents feel a dependency's failure)
"""

from __future__ import annotations

import json
from functools import lru_cache

# group -> default criticality for unknown classes in that group
GROUP_CRITICALITY = {
    "health_education": 0.9,
    "energy": 0.85,
    "communication": 0.7,
    "transport": 0.6,
    "roads": 0.55,
    "water": 0.5,
}

# group -> base structural vulnerability to flood/heat/wind (0..1)
GROUP_VULNERABILITY = {
    "health_education": 0.45,
    "energy": 0.55,
    "communication": 0.6,
    "transport": 0.5,
    "roads": 0.55,
    "water": 0.35,
}

# OSM class -> criticality overrides
CLASS_CRITICALITY = {
    "hospital": 0.98, "clinic": 0.8, "doctors": 0.65, "pharmacy": 0.4,
    "fire_station": 0.9, "police": 0.85,
    "kindergarten": 0.6, "school": 0.65, "college": 0.7, "university": 0.75,
    "substation": 0.9, "plant": 0.95, "generator": 0.85, "transformer": 0.8,
    "station": 0.7, "halt": 0.4, "tram_stop": 0.4,
    "aerodrome": 0.95, "helipad": 0.7, "runway": 0.9, "terminal": 0.8,
    "mast": 0.75, "communication_tower": 0.8,
    "water_works": 0.85, "wastewater_plant": 0.7,
}

# highway class -> (criticality, vulnerability) for road segments
ROAD_CLASS_PARAMS = {
    "motorway": (0.95, 0.4), "motorway_link": (0.8, 0.4),
    "trunk": (0.9, 0.45), "trunk_link": (0.75, 0.45),
    "primary": (0.8, 0.5), "primary_link": (0.6, 0.5),
    "secondary": (0.65, 0.55), "secondary_link": (0.5, 0.55),
    "tertiary": (0.55, 0.6), "tertiary_link": (0.45, 0.6),
    "residential": (0.35, 0.65), "living_street": (0.25, 0.65),
    "service": (0.2, 0.7), "unclassified": (0.3, 0.65),
    "footway": (0.1, 0.75), "path": (0.1, 0.75), "pedestrian": (0.15, 0.7),
    "track": (0.1, 0.8),
}


def _tags(row) -> dict:
    tags = row.get("tags")
    if isinstance(tags, str) and tags:
        try:
            return json.loads(tags)
        except json.JSONDecodeError:
            return {}
    if isinstance(tags, dict):
        return tags
    return {}


@lru_cache(maxsize=4096)
def _cached_class_params(group: str, category: str, highway: str | None,
                         amenity: str | None, power: str | None, railway: str | None,
                         aeroway: str | None, tower_type: str | None,
                         man_made: str | None, emergency: str | None,
                         bridge: str | None = None, tunnel: str | None = None,
                         cutting: str | None = None, embankment: str | None = None) -> dict:
    tags = {
        "highway": highway, "amenity": amenity, "power": power,
        "railway": railway, "aeroway": aeroway, "tower:type": tower_type,
        "man_made": man_made, "emergency": emergency,
        "bridge": bridge, "tunnel": tunnel, "cutting": cutting,
        "embankment": embankment,
    }
    # criticality
    crit = GROUP_CRITICALITY.get(group, 0.4)
    if group == "roads" and highway in ROAD_CLASS_PARAMS:
        crit = ROAD_CLASS_PARAMS[highway][0]
    for key in (amenity, power, railway, aeroway, man_made, emergency):
        if key and key in CLASS_CRITICALITY:
            crit = max(crit, CLASS_CRITICALITY[key])
            break
    if power == "line" or power == "minor_line":
        crit = max(crit, 0.6)
    if category == "bridges":
        crit = max(crit, 0.85)  # bridges are network single points of failure
    if category == "railways":
        crit = max(crit, 0.6)

    # vulnerability (flood baseline; heat/wind modifiers applied by engine)
    vuln = GROUP_VULNERABILITY.get(group, 0.5)
    if group == "roads" and highway in ROAD_CLASS_PARAMS:
        vuln = ROAD_CLASS_PARAMS[highway][1]
    if tags.get("bridge"):
        vuln = min(0.85, vuln + 0.2)      # bridges scour
    if tags.get("tunnel"):
        vuln = min(0.9, vuln + 0.25)      # tunnels flood
    if tags.get("cutting") == "yes":
        vuln = min(0.9, vuln + 0.15)
    if tags.get("embankment"):
        vuln = max(0.2, vuln - 0.1)       # raised above surroundings
    if amenity in ("hospital", "fire_station", "police"):
        vuln = min(vuln, 0.35)            # built to higher standards
    if power in ("plant", "substation"):
        vuln = min(0.4, vuln)             # protected installations
    # elevation-aware adjustment is applied by the engine using DEM data.
    return {"criticality": round(crit, 3), "vulnerability": round(vuln, 3)}


def asset_params(row) -> dict:
    """Criticality/vulnerability for one asset row (pandas Series or dict)."""
    tags = _tags(row)
    return _cached_class_params(
        str(row.get("group_name", "")),
        str(row.get("category", "")),
        tags.get("highway"), tags.get("amenity"), tags.get("power"),
        tags.get("railway"), tags.get("aeroway"), tags.get("tower:type"),
        tags.get("man_made"), tags.get("emergency"),
        bridge=tags.get("bridge"), tunnel=tags.get("tunnel"),
        cutting=tags.get("cutting"), embankment=tags.get("embankment"),
    )


def elevation_vulnerability(vulnerability: float, ground_elev_m: float,
                            aoi_min_elev_m: float, aoi_max_elev_m: float) -> float:
    """Low-lying assets are structurally more flood-vulnerable (0..1 blend)."""
    if aoi_max_elev_m <= aoi_min_elev_m:
        return vulnerability
    rel = (ground_elev_m - aoi_min_elev_m) / (aoi_max_elev_m - aoi_min_elev_m)
    elev_factor = 1.0 - min(max(rel, 0.0), 1.0)  # 1 at lowest point, 0 at highest
    return float(min(1.0, vulnerability * (0.7 + 0.6 * elev_factor)))
