"""Live-server verification that the cascade + flood fixes changed output."""
import json
import urllib.request


def post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:8600{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


for hz, p, depth_key in (
    ("flood", {"rainfall_mm": 300, "duration_h": 24}, "max_depth_m"),
    ("cyclone", {"category": 3}, "max_surge_m"),
):
    d = post("/api/simulate", {"hazard_type": hz, "params": p})
    s = d["summary"]
    cascade_only = [a for a in d["assets"] if a["cascade_impact"] > 0 and a["exposure"] < 0.05]
    downstream = [a for a in d["assets"] if a["impact_depth"] > 0]
    sources = [a for a in d["assets"] if a["cascade_impact"] >= 1.0]
    print(
        f"{hz}: exposed={s['assets_exposed']} high_risk={s['assets_high_risk']} "
        f"pop={s['affected_population']} {depth_key}={s['hazard'].get(depth_key)} "
        f"cascade_only_assets={len(cascade_only)} downstream={len(downstream)} "
        f"sources={len(sources)} edges={len(d['cascade_edges'])}"
    )
    assert s["assets_exposed"] > 0, f"{hz}: nothing exposed"
    assert sources, f"{hz}: no cascade sources"
    # flood must leave unexposed dependents that still feel the cascade;
    # a direct cyclone hit fails nearly everything, so its cascade set has
    # no separate downstream (checked via sources instead)
    if hz == "flood":
        assert cascade_only, "flood: no cascade-only assets"
print("VERIFICATION OK")
