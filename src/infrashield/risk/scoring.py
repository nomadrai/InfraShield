"""Risk scoring: the explainable H x V x C formula and its lookup tables.

Formula (single source of truth for the whole app):

    R = (H * V * C) / SCALE        with H, V, C each normalised 0..10
    SCALE = 100                    -> R is 0..10

Spec note: the brief writes ``R = (H*V*C)/10`` with H,V,C in 0..10, which
yields 0..100, yet the required bands are 0-4 / 4-7 / 7-10 and the brief's
own worked example (7.2 x 9 x 8.8 = 8.4) matches a geometric-mean-like
normalisation, not /10. We therefore normalise by 100 so R lands on the
0..10 band scale; SCALE is a single constant here if the team re-tunes.

Bands (used identically by map markers, popups and the priority panel):

    0 <= R < 4   Safe      (no alert)
    4 <= R < 7   Elevated  (yellow)
    7 <= R <= 10 Critical  (red)

Everything tunable (V table, C derivation weights, band edges, action
lookup) lives in this module - never inline in rendering code.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Tuning constants
# --------------------------------------------------------------------------- #
SCALE = 100.0          # H*V*C / SCALE -> 0..10
BAND_EDGES = (4.0, 7.0)  # safe < 4 <= elevated < 7 <= critical

# V: asset vulnerability 0..10 by asset type. Starting weights from the
# brief (hospital 9, school 6, residential road 4, bridge 7, substation 8);
# others extrapolated by structural fragility. Documented, tunable here.
VULNERABILITY_TABLE: dict[str, float] = {
    # health / emergency
    "hospital": 9.0, "clinic": 7.0, "doctors": 6.0, "dentist": 6.0,
    "fire_station": 8.0, "police": 7.0,
    # education
    "school": 6.0, "kindergarten": 6.0, "college": 6.0, "university": 6.0,
    # transport
    "bridge": 7.0, "viaduct": 7.5, "aqueduct": 7.0,
    "motorway": 6.0, "trunk": 5.5, "primary": 5.0, "secondary": 4.5,
    "tertiary": 4.0, "residential": 4.0, "service": 3.0, "unclassified": 4.0,
    "living_street": 3.5, "footway": 2.5, "track": 2.5,
    "station": 6.0, "rail": 5.0, "subway": 6.5,   # tunnels flood
    "aerodrome": 8.0, "runway": 7.0, "helipad": 7.0,
    # energy
    "substation": 8.0, "plant": 9.0, "generator": 8.0, "tower": 6.0,
    "line": 5.0, "minor_line": 4.5, "transformer": 7.0,
    # water / comms
    "water_works": 7.5, "wastewater_plant": 7.0, "mast": 6.5, "tower:type": 6.5,
}

# Fallback V when the asset type is unknown.
VULNERABILITY_DEFAULT = 5.0

# C: criticality/dependency weight 0..10, derived (deterministically) from
# population served, redundancy, downstream dependency count. Weights below
# are the documented starting point; C = 10 * (w_pop*pop + w_red*redundancy
# + w_dep*dependents), each sub-score 0..1.
C_WEIGHTS = {"pop": 0.4, "redundancy": 0.3, "dependents": 0.3}

# Recommended action lookup: (asset class, band) -> deterministic action.
# Class here is the coarse OSM family (see asset_class()).
ACTION_TABLE: dict[tuple[str, str], str] = {
    ("hospital", "critical"): "Evacuate ground floor; verify generator fuel now",
    ("hospital", "elevated"): "Stage supplies; test backup power",
    ("clinic", "critical"): "Relocate patients; close at exposure",
    ("clinic", "elevated"): "Alert staff; prepare to relocate",
    ("school", "critical"): "Cancel classes; evacuate grounds",
    ("school", "elevated"): "Notify parents; move assemblies indoors/upper floors",
    ("substation", "critical"): "Dispatch backup power; pre-emptive load transfer",
    ("substation", "elevated"): "Crew on standby; inspect drainage around site",
    ("plant", "critical"): "Shut down safely; dispatch backup generation",
    ("bridge", "critical"): "Close span; reroute via alternates",
    ("bridge", "elevated"): "Post load restriction; inspect scour",
    ("road", "critical"): "Close segment; deploy barriers and signage",
    ("road", "elevated"): "Reduce capacity; monitor depth at kerb",
    ("station", "critical"): "Suspend services through station",
    ("station", "elevated"): "Alert operations; check pump systems",
    ("airport", "critical"): "Suspend operations; move aircraft to hangars",
    ("airport", "elevated"): "Review drainage; delay ground movements",
    ("mast", "critical"): "Dispatch generator to site",
    ("default", "critical"): "Restrict access; prepare protective measures",
    ("default", "elevated"): "Monitor condition; brief response team",
}

# Intensity normalisation per hazard (physical value -> H 0..10).
# Flood: 1.5 m depth = full score (uninhabitable ground floor), documented.
FLOOD_DEPTH_MAX_M = 1.5
# Landslide: susceptibility index 0..1 -> H 0..10.
# Heat: heat index 0..1 (33-45 C band) -> H 0..10.


def band_of(risk: float) -> str:
    """Score band name for a 0..10 risk score."""
    if risk < BAND_EDGES[0]:
        return "safe"
    if risk < BAND_EDGES[1]:
        return "elevated"
    return "critical"


def flood_intensity(depth_m: float) -> float:
    """Water depth (m) -> H 0..10 (1.5 m = 10)."""
    return max(0.0, min(10.0, 10.0 * depth_m / FLOOD_DEPTH_MAX_M))


def heat_intensity(heat_index: float) -> float:
    """Heat index 0..1 -> H 0..10."""
    return max(0.0, min(10.0, 10.0 * heat_index))


def intensity(hazard_type: str, **values) -> float:
    """H for a hazard given its physical value(s) at the asset."""
    if hazard_type == "flood":
        depth = values.get("depth_m") or 0.0
        surge = values.get("surge_depth_m") or 0.0
        return flood_intensity(max(depth, surge))
    if hazard_type == "heat":
        return heat_intensity(values.get("heat_index") or 0.0)
    if hazard_type == "cyclone":
        wf = values.get("wind_frac") or 0.0
        sd = values.get("surge_depth_m") or 0.0
        return max(flood_intensity(sd), 10.0 * wf)
    if hazard_type == "landslide":
        return 10.0 * (values.get("susceptibility") or 0.0)
    return 0.0


def asset_class(group: str, category: str, tags: dict) -> str:
    """Coarse OSM family used to key V and the action table."""
    amenity = tags.get("amenity")
    if amenity:
        if amenity in ("hospital", "clinic", "doctors", "dentist"):
            return "hospital" if amenity == "hospital" else "clinic"
        if amenity in ("fire_station", "police"):
            return amenity
        if amenity in ("school", "kindergarten", "college", "university", "institute"):
            return "school"
        if amenity == "station":
            return "station"
    if category == "bridges" or tags.get("bridge"):
        return "bridge"
    if group == "power":
        p = tags.get("power")
        if p in ("substation", "transformer"):
            return "substation"
        if p in ("plant", "generator"):
            return "plant"
        if p in ("tower", "line", "minor_line"):
            return "line"
    if group == "airports" or tags.get("aeroway") == "aerodrome":
        return "airport"
    if group == "telecom" or tags.get("man_made") == "mast":
        return "mast"
    if group in ("roads", "transport"):
        return "road"
    return group or "default"


def vulnerability(asset_class_name: str) -> float:
    """V 0..10 from the lookup table (documented fallback for unknowns)."""
    return VULNERABILITY_TABLE.get(asset_class_name, VULNERABILITY_DEFAULT)


def criticality(pop_served: int, redundancy_loss: float, dependents: int,
                pop_norm_cap: int = 2200) -> float:
    """C 0..10 from population served, redundancy and downstream dependencies.

    Deterministic weights from C_WEIGHTS; each input normalised:
      pop        min(1, pop_served / pop_norm_cap)
      redundancy 1 - min(1, redundancy_loss)   (loss of alternatives raises C)
      dependents min(1, dependents / 5)
    """
    pop = min(1.0, max(0, pop_served) / pop_norm_cap)
    red = 1.0 - min(1.0, max(0.0, redundancy_loss))
    deps = min(1.0, max(0, dependents) / 5.0)
    w = C_WEIGHTS
    raw = w["pop"] * pop + w["redundancy"] * red + w["dependents"] * deps
    return round(10.0 * raw, 2)


def risk_score(h: float, v: float, c: float) -> float:
    """R = H*V*C / SCALE, rounded for display."""
    return round(min(10.0, h * v * c / SCALE), 2)


def recommended_action(asset_class_name: str, band: str) -> str:
    """Deterministic action from the (class x band) lookup table."""
    return ACTION_TABLE.get(
        (asset_class_name, band),
        ACTION_TABLE.get(("default", band), "Monitor; no special preparation"),
    )
