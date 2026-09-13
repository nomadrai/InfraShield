/* InfraShield dashboard — all data comes from the real API/engine. */
"use strict";

/* ------------------------------------------------------------------ */
/* state                                                               */
/* ------------------------------------------------------------------ */
const S = {
  meta: null,
  layers: {},          // group -> GeoJSON data
  assetLayers: {},     // group -> L.GeoJSON
  coordIndex: {},      // asset_id -> [lat, lon]
  sim: null,           // last simulation payload
  assetState: new Map(), // asset_id -> assessment dict
  pointClusters: {},   // group -> L.markerClusterGroup
  pointZoomBound: {},  // group -> min zoom for visibility
  plainGeoms: {},      // group -> raw GeoJSON (icons rebuilt on zoom)
  stageIdx: 0,
  playing: false,
  playTimer: null,
  stageCanvases: [],   // decoded+colored dataURLs per stage
  hazardOverlay: null,
  cascadeLayer: null,
  landfallMarker: null,
  landfall: null,      // {lon, lat} for cyclone
  selectedId: null,
  showCascade: false,
};

const $ = (sel) => document.querySelector(sel);
const fmt = {
  int: (n) => (n ?? 0).toLocaleString("en-US"),
  pct: (x) => `${Math.round((x ?? 0) * 100)}%`,
  pop: (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n ?? 0}`),
  score: (x) => (x ?? 0).toFixed(2),
};

/* ------------------------------------------------------------------ */
/* map                                                                 */
/* ------------------------------------------------------------------ */
const map = L.map("map", { zoomControl: true, preferCanvas: false })
  .setView([19.13, 72.91], 13);

// Basemap: CARTO dark tiles when a CARTO_BASEMAP_KEY is configured in .env
// (surfaced by /api/meta as meta.basemap.api_key); otherwise fall back to
// OpenFreeMap's dark style (free, no key, no watermark).
// Basemaps: Esri World Imagery (satellite, free, no key) is the default.
// OSM Standard and CARTO dark (key via .env, injected by the server) are
// one click away in the basemap switcher. All raster tile endpoints.
const BASEMAPS = {
  satellite: {
    label: "Satellite",
    layer: () => L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      { attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics", maxZoom: 19 }),
  },
  osm: {
    label: "OSM Standard",
    layer: () => L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19 }),
  },
  dark: {
    label: "Dark",
    layer: () => {
      const key = (window.__basemapKey || "").trim();
      const url = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        + (key ? "?key=" + encodeURIComponent(key) : "");
      return L.tileLayer(url, {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: "abcd", maxZoom: 20 });
    },
  },
};
let basemapName = "satellite";
let basemapLayer = null;

function setBasemap(name) {
  const spec = BASEMAPS[name];
  if (!spec) return;
  if (basemapLayer) map.removeLayer(basemapLayer);
  basemapLayer = spec.layer().addTo(map);
  basemapLayer.bringToBack();
  basemapName = name;
  for (const b of document.querySelectorAll(".basemap-btn"))
    b.classList.toggle("active", b.dataset.basemap === name);
}

map.on("zoomend", applyZoomGating);
setBasemap("satellite");

/* ------------------------------------------------------------------ */
/* cartographic point symbols (one distinct shape+color per category)   */
/* ------------------------------------------------------------------ */
/* SVG paths drawn on a 24x24 viewBox; each kind has a unique silhouette
   AND hue so categories never collapse visually, even in grayscale.   */
const SYMBOLS = {
  hospital:    { label: "Hospital", color: "#dc2626", ring: "#fecaca", shape: '<path d="M12 4h2v6h6v2h-6v6h-2v-6H6v-2h6z" fill="#dc2626"/>' },   // red cross
  clinic:      { label: "Clinic / doctor", color: "#f87171", ring: "#fee2e2", shape: '<path d="M12 5h1.6v5.4H19V12h-5.4v5.4H12V12H6.6v-1.6H12z" fill="#f87171"/>' }, // small red cross
  fire:        { label: "Fire station", color: "#ea580c", ring: "#ffedd5", shape: '<path d="M12 3c2 3-1 4.5 1.5 6.5C15.6 11.2 17 9.6 16 7c3 2 4 5.2 4 7.4A8 8 0 0 1 4 14.5C4 10 8 8 9 5c.6 1.6 2 2.4 3-2zM12 19a3.2 3.2 0 0 0 3.2-3.2c0-1.7-1.6-2.9-3.2-4.6-1.6 1.7-3.2 2.9-3.2 4.6A3.2 3.2 0 0 0 12 19z" fill="#ea580c"/>' }, // flame
  police:      { label: "Police", color: "#2563eb", ring: "#dbeafe", shape: '<path d="M12 3l7 2.4v5.2c0 4.6-3 8-7 10.4-4-2.4-7-5.8-7-10.4V5.4L12 3zm-1.6 12.2l6-6-1.4-1.4-4.6 4.6-2-2-1.4 1.4 3.4 3.4z" fill="#2563eb"/>' }, // shield
  school:      { label: "School", color: "#d97706", ring: "#fef3c7", shape: '<path d="M12 4L2 9l10 5 8-4v5h2V9L12 4zm-6 8.6V16c0 1.7 2.7 3 6 3s6-1.3 6-3v-3.4l-6 3-6-3z" fill="#d97706"/>' }, // graduation cap
  substation:  { label: "Substation / power", color: "#eab308", ring: "#fef08a", shape: '<path d="M13 2L5 13h5l-1.5 9L19 10h-5.5L13 2z" fill="#eab308" stroke="#422006" stroke-width="1.2"/>' }, // lightning bolt
  pylon:       { label: "Transmission tower", color: "#a16207", ring: "#fef9c3", shape: '<path d="M9 3h6l-1 4h2v2h-2.6L15 13h2v2h-2.7L16 21h-2.2l-1-4h-3.6l-1 4H6l1.7-6H5v-2h2l1.6-4H6V5h2L9 3zm1.4 4L9.6 11h4.8l-.8-4h-3.2z" fill="#a16207"/>' }, // lattice tower
  station:     { label: "Rail station", color: "#334155", ring: "#e2e8f0", shape: '<path d="M12 3a5 5 0 0 1 5 5v6a5 5 0 0 1-2.5 4.3L16 21h-2.4l-1.2-2h-1l-1.2 2H8l1.5-2.7A5 5 0 0 1 7 14V8a5 5 0 0 1 5-5zm-3 5v4h6V8H9zm3 8.4a1.4 1.4 0 1 0 0 2.8 1.4 1.4 0 0 0 0-2.8z" fill="#334155"/>' }, // train front
  mast:        { label: "Telecom mast", color: "#0891b2", ring: "#cffafe", shape: '<path d="M11 8h2v13h-2V8zm1-5l3 4h-6l3-4zM6 9c0 3 1.4 4.4 3 5v2c-2.8-.7-5-3-5-7h2zm12 0h2c0 4-2.2 6.3-5 7v-2c1.6-.6 3-2 3-5z" fill="#0891b2"/>' }, // mast with waves
  airport:     { label: "Airport / airfield", color: "#7c3aed", ring: "#ede9fe", shape: '<path d="M21 15l-8-4V4.5a1.5 1.5 0 0 0-3 0V11l-8 4v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-4.5l8 2.5v-2z" fill="#7c3aed"/>' }, // plane
  bridge_pt:   { label: "Bridge", color: "#475569", ring: "#e2e8f0", shape: '<path d="M3 13h2v3H3v-3zm16 0h2v3h-2v-3zM2 12c4-5 16-5 20 0h-3c-3.5-3.4-10.5-3.4-14 0H2z" fill="#475569"/>' }, // arch span
};

/* map (layer group, OSM tag) -> symbol kind */
function pointKind(group, tags) {
  const amenity = tags.amenity, power = tags.power, manMade = tags.man_made,
        railway = tags.railway, aeroway = tags.aeroway;
  if (group === "hospitals") {
    if (amenity === "hospital") return "hospital";
    if (amenity === "fire_station") return "fire";
    if (amenity === "police") return "police";
    if (amenity === "clinic" || amenity === "doctors" || amenity === "dentist") return "clinic";
    return "clinic";
  }
  if (group === "schools") {
    if (amenity === "school" || amenity === "kindergarten" || amenity === "college"
        || amenity === "university" || amenity === "institute") return "school";
    return "school";
  }
  if (group === "power") {
    if (power === "tower") return "pylon";
    if (power === "substation" || power === "plant" || power === "generator"
        || manMade === "mast" || power === "line" || power === "minor_line") return "substation";
    return "substation";
  }
  if (group === "railways") return "station";
  if (group === "telecom") return "mast";
  if (group === "airports") return "airport";
  if (group === "bridges") return "bridge_pt";
  return null; // not a point category (roads stay lines)
}

/* zoom policy: individual icons + clusters appear past these zooms.
   Below the threshold the layer is hidden entirely (region-wide view);
   clusters (count bubbles) show from minZoom; individual icons from
   ICON_ZOOM (dense pylons need a higher threshold than hospitals). */
const POINT_CFG = {
  hospitals: { minZoom: 12 },
  schools:   { minZoom: 12 },
  power:     { minZoom: 14 },   // 315 pylons: keep out until neighbourhood view
  railways:  { minZoom: 12 },
  telecom:   { minZoom: 12 },
  airports:  { minZoom: 12 },
  bridges:   { minZoom: 13 },
};
const ICON_ZOOM = 15;           // >= this: individual icons (clusters before)

function makeIcon(kind, stateKey) {
  const sym = SYMBOLS[kind] || SYMBOLS.clinic;
  const ring = stateKey === "failed" ? "#ef4444"
    : stateKey === "exposed" ? "#f59e0b"
    : stateKey === "cascade" ? "#fb923c" : sym.ring;
  const cls = "infra-marker" + (stateKey ? " st-" + stateKey : "");
  return L.divIcon({
    className: cls,
    html: `<span class="marker-ring" style="border-color:${ring}"></span>` +
          `<svg viewBox="0 0 24 24" width="19" height="19">${sym.shape}</svg>`,
    iconSize: [23, 23], iconAnchor: [11, 11],
  });
}

/* group draw order: roads first, facilities on top */
const GROUP_ORDER = ["roads_minor", "roads_major", "railways", "power", "telecom",
  "waterways_ctx", "bridges", "airports", "schools", "hospitals"];
const GROUP_STYLE = {
  hospitals:        { color: "#f87171", weight: 2, opacity: 0.9 },
  schools:          { color: "#fbbf24", weight: 2, opacity: 0.85 },
  power:            { color: "#facc15", weight: 1.6, opacity: 0.8 },
  telecom:          { color: "#22d3ee", weight: 2, opacity: 0.9 },
  airports:         { color: "#a78bfa", weight: 2, opacity: 0.85 },
  railways:         { color: "#94a3b8", weight: 1.5, opacity: 0.6, dashArray: "5 4" },
  bridges:          { color: "#34d399", weight: 2.2, opacity: 0.85 },
  roads_major:      { color: "#64748b", weight: 2.2, opacity: 0.75 },
  roads_minor:      { color: "#475569", weight: 1.1, opacity: 0.55 },
};

function baseStyle(group) {
  return { ...(GROUP_STYLE[group] || { color: "#64748b", weight: 1.5, opacity: 0.6 }) };
}

/* ------------------------------------------------------------------ */
/* bootstrap                                                           */
/* ------------------------------------------------------------------ */
/* null-safe DOM helpers: a missing element must never kill the app */
function setText(sel, text) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (el) el.textContent = text;
}
function setHTML(sel, html) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (el) el.innerHTML = html;
}

async function boot() {
  try {
    const [meta, layers] = await Promise.all([
      fetch("/api/meta").then((r) => {
        if (!r.ok) throw new Error(`/api/meta ${r.status}`);
        return r.json();
      }),
      fetch("/api/layers").then((r) => {
        if (!r.ok) throw new Error(`/api/layers ${r.status}`);
        return r.json();
      }),
    ]);
    S.meta = meta;
    S.layers = layers;

    setText("#study-name", `${meta.study_area.name} · DEM ${meta.study_area.elevation_range_m[0]}–${meta.study_area.elevation_range_m[1]} m · ${meta.study_area.grid.cell_size_m} m grid`);
    const counts = Object.entries(meta.counts || {}).map(([k, v]) => `<b>${fmt.int(v)}</b> ${k.replace("_", " ")}`).join(" · ");
    setHTML("#header-meta", counts);

    buildAssetLayers();
    buildHazardPicker();
  } catch (err) {
    console.error("boot failed:", err);
    toast(`Failed to load backend data: ${err.message}`);
    setStatus("error", "Backend unreachable");
  }
}

function featureId(f) {
  return f.id ?? (f.properties?.osm_type + "/" + f.properties?.osm_id);
}

function safeTags(raw) {
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch { return {}; }
}

function rememberCoords(f) {
  const c = f.geometry?.coordinates;
  if (!c) return;
  const pt = Array.isArray(c[0]) ? c[Math.floor(c.length / 2)] : c;
  S.coordIndex[featureId(f)] = Array.isArray(pt[0]) ? [pt[0][1], pt[0][0]] : [pt[1], pt[0]];
}

function buildAssetLayers() {
  for (const [group, gj] of Object.entries(S.layers)) {
    if (!GROUP_STYLE[group]) continue;
    S.plainGeoms[group] = gj;

    if (group in POINT_CFG) {
      // ---- point categories: clustered divIcon markers ----
      const markers = [];
      for (const f of gj.features) {
        if (f.geometry?.type !== "Point") continue;
        const kind = pointKind(group, safeTags(f.properties?.tags));
        if (!kind) continue;
        const id = featureId(f);
        const [lon, lat] = f.geometry.coordinates;
        const mk = L.marker([lat, lon], { icon: makeIcon(kind, null), title: f.properties?.name || id });
        mk.__assetId = id; mk.__kind = kind;
        mk.on("click", () => selectAsset(id, mk));
        markers.push(mk);
        rememberCoords(f);
      }
      const cluster = L.markerClusterGroup({
        maxClusterRadius: 44,
        showCoverageOnHover: false,
        spiderfyDistanceMultiplier: 1.6,
        iconCreateFunction: (c) => L.divIcon({
          html: `<span class="cluster-bubble">${c.getChildCount()}</span>`,
          className: "infra-cluster", iconSize: [34, 34],
        }),
      });
      cluster.addLayers(markers);
      S.pointClusters[group] = cluster;
      S.pointZoomBound[group] = POINT_CFG[group].minZoom;
    } else {
      // ---- line/area categories: geoJSON layer as before ----
      const layer = L.geoJSON(gj, {
        style: () => styleFor(group, null),
        onEachFeature: (feature, lyr) => {
          lyr.on("click", () => selectAsset(feature.id ?? feature.properties.osm_type + "/" + feature.properties.osm_id, lyr));
          lyr.on("mouseover", () => lyr.setStyle({ weight: (GROUP_STYLE[group]?.weight || 1.5) + 1.5, opacity: 1 }));
          lyr.on("mouseout", () => lyr.setStyle(styleFor(group, S.assetState.get(feature.id))));
          rememberCoords(feature);
        },
      });
      S.assetLayers[group] = layer;
    }
  }
  // add in draw order
  for (const g of GROUP_ORDER) {
    if (S.pointClusters[g]) map.addLayer(S.pointClusters[g]);
    else if (S.assetLayers[g]) S.assetLayers[g].addTo(map);
  }
  applyZoomGating();
}

/* ------------------------------------------------------------------ */
/* styling driven by simulation results                                */
/* ------------------------------------------------------------------ */
const RISK_COLOR = (r) =>
  r >= 0.6 ? "#ef4444" : r >= 0.4 ? "#f97316" : r >= 0.2 ? "#fbbf24" : "#fde68a";

function markerState(a) {
  if (!a) return null;
  if (a.failed === true) return "failed";
  if (a.exposure > 0.4) return "exposed";
  if (a.risk >= 0.15 || a.cascade_impact > 0) return "cascade";
  return null;
}

function stateRing(state) {
  return state === "failed" ? "#ef4444"
    : state === "exposed" ? "#f59e0b"
    : state === "cascade" ? "#fb923c" : null;
}

/* re-icon markers to reflect simulation state (ring color) */
function refreshMarkerIcons() {
  for (const cluster of Object.values(S.pointClusters)) {
    cluster.eachLayer((mk) => {
      if (!mk.__assetId) return;
      const st = markerState(S.assetState.get(mk.__assetId));
      if (mk.__state !== st) {
        mk.setIcon(makeIcon(mk.__kind, st));
        mk.__state = st;
      }
    });
  }
}

/* zoom gating: point layers visible only at/above their minZoom */
function applyZoomGating() {
  const z = map.getZoom();
  for (const [group, cluster] of Object.entries(S.pointClusters)) {
    const min = S.pointZoomBound[group] ?? 12;
    const visible = z >= min;
    const currently = map.hasLayer(cluster);
    if (visible && !currently) map.addLayer(cluster);
    else if (!visible && currently) map.removeLayer(cluster);
  }
  renderLegend();
}

function markerStateForStage(a, stageIdx) {
  if (!a || stageIdx === null || stageIdx === undefined || !a.stage_exposure?.length) return markerState(a);
  const se = a.stage_exposure[Math.min(stageIdx, a.stage_exposure.length - 1)] ?? 0;
  if (se >= 0.55) return "failed";
  if (se >= 0.05) return "exposed";
  return null;
}

function styleFor(group, a) {
  // no simulation yet (or unaffected asset): dim base style
  if (!a) {
    const s = baseStyle(group);
    if (S.sim) { s.opacity = (s.opacity ?? 0.6) * 0.45; } // dim unaffected after a run
    return s;
  }
  const failed = a.failed;
  const exposed = a.exposure > 0.05;
  const base = baseStyle(group);
  if (failed) {
    return { ...base, color: "#ef4444", weight: base.weight + 1.2, opacity: 1,
      fillOpacity: 0.85, dashArray: "3 3" };
  }
  if (a.risk >= 0.15 || a.cascade_impact > 0) {
    return { ...base, color: RISK_COLOR(a.risk || a.cascade_impact),
      weight: base.weight + 0.8, opacity: 1 };
  }
  if (exposed) {
    return { ...base, color: "#fde68a", weight: base.weight, opacity: 0.9 };
  }
  base.opacity = (base.opacity ?? 0.6) * 0.45;
  return base;
}

function refreshStyles(stageIdx = null) {
  for (const [group, layer] of Object.entries(S.assetLayers)) {
    layer.eachLayer((lyr) => {
      const id = lyr.feature?.id;
      let a = S.assetState.get(id) || null;
      if (a && stageIdx !== null && a.stage_exposure?.length) {
        // stage-specific view: does the hazard reach the asset at this stage?
        const se = a.stage_exposure[Math.min(stageIdx, a.stage_exposure.length - 1)] ?? 0;
        const isRoad = group.startsWith("roads") || group === "railways";
        const failT = isRoad ? 0.4 : 0.55; // mirrors engine failure thresholds
        a = { ...a, failed: se >= failT, exposure: Math.max(a.exposure * (se >= 0.05 ? 1 : 0), se) };
      }
      lyr.setStyle(styleFor(group, a));
    });
  }
  // point markers follow the same stage logic via ring color
  if (stageIdx !== null) {
    for (const cluster of Object.values(S.pointClusters)) {
      cluster.eachLayer((mk) => {
        if (!mk.__assetId) return;
        const a = S.assetState.get(mk.__assetId);
        const st = markerStateForStage(a, stageIdx);
        if (mk.__state !== st) {
          mk.setIcon(makeIcon(mk.__kind, st));
          mk.__state = st;
        }
      });
    }
  } else {
    refreshMarkerIcons();
  }
}

/* ------------------------------------------------------------------ */
/* hazard picker + params                                              */
/* ------------------------------------------------------------------ */
const HAZARD_ICONS = { flood: "🌊", heat: "🔥", cyclone: "🌀" };

function buildHazardPicker() {
  const picker = $("#hazard-picker");
  picker.innerHTML = "";
  for (const [key, h] of Object.entries(S.meta.hazards)) {
    const btn = document.createElement("button");
    btn.dataset.hazard = key;
    btn.innerHTML = `<span class="ico">${HAZARD_ICONS[key] || "❗"}</span>${h.label.split(" ")[0]}`;
    btn.onclick = () => selectHazard(key);
    picker.appendChild(btn);
  }
  selectHazard("flood");
}

function hazardSpec() {
  const key = $("#hazard-picker .active")?.dataset.hazard || "flood";
  return [key, S.meta.hazards[key]];
}

function selectHazard(key) {
  for (const b of $("#hazard-picker").children) b.classList.toggle("active", b.dataset.hazard === key);
  const spec = S.meta.hazards[key];
  const params = Object.entries(spec.params);
  const [pKey, pSpec] = params[0];
  const slider = $("#severity");
  slider.min = pSpec.min; slider.max = pSpec.max; slider.step = pSpec.step; slider.value = pSpec.default;
  setText("#severity-label", pSpec.label);
  updateSeverityLabels();

  const sField = $("#secondary-field");
  if (params[1]) {
    const [sKey, sSpec] = params[1];
    sField.classList.remove("hidden");
    const s2 = $("#secondary");
    s2.min = sSpec.min; s2.max = sSpec.max; s2.step = sSpec.step; s2.value = sSpec.default;
    $("#secondary-label").textContent = sSpec.label;
  } else {
    sField.classList.add("hidden");
  }
  if (key === "cyclone") {
    setText("#secondary-note", "Tip: click the map to move the landfall point (default offshore).");
  } else {
    setText("#secondary-note", "");
  }
}

function currentParams() {
  const params = { };
  const [key] = hazardSpec();
  const pSpec = S.meta.hazards[key].params;
  const keys = Object.keys(pSpec);
  params[keys[0]] = parseFloat($("#severity").value);
  if (keys[1]) params[keys[1]] = parseFloat($("#secondary").value);
  if (key === "cyclone" && S.landfall) {
    params.landfall_lon = +S.landfall.lng.toFixed(4);
    params.landfall_lat = +S.landfall.lat.toFixed(4);
  }
  return params;
}

function updateSeverityLabels() {
  const [key] = hazardSpec();
  const params = S.meta?.hazards[key]?.params;
  if (!params) return;
  const keys = Object.keys(params);
  setText("#severity-out", $("#severity").value + (keys[0] === "rainfall_mm" ? " mm" : keys[0] === "anomaly_c" ? " °C" : ""));
  if (keys[1]) setText("#secondary-out", $("#secondary").value);
}

$("#severity").addEventListener("input", updateSeverityLabels);

/* cyclone landfall: click the map */
map.on("click", (e) => {
  const [key] = hazardSpec();
  if (key !== "cyclone") return;
  S.landfall = e.latlng;
  if (S.landfallMarker) S.landfallMarker.setLatLng(e.latlng);
  else S.landfallMarker = L.circleMarker(e.latlng, { radius: 9, color: "#ec4899", weight: 2, fillOpacity: 0.25 })
    .addTo(map).bindTooltip("Cyclone landfall");
});

/* ------------------------------------------------------------------ */
/* run / reset                                                         */
/* ------------------------------------------------------------------ */
function setStatus(state, text) {
  const el = $("#sim-status");
  el.dataset.state = state;
  $("#sim-status-text").textContent = text;
}

async function runSimulation() {
  const [hazard] = hazardSpec();
  const params = currentParams();
  $("#btn-run").disabled = true;
  $("#btn-run").classList.add("running");
  $("#progress").classList.remove("hidden");
  setStatus("running", `Simulating ${hazard}…`);

  try {
    const t0 = performance.now();
    const res = await fetch("/api/simulate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hazard_type: hazard, params }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || res.statusText);
    }
    const sim = await res.json();
    sim._elapsed_hint = ((performance.now() - t0) / 1000).toFixed(1);
    loadSimulation(sim);
  } catch (err) {
    toast(`Simulation failed: ${err.message}`);
    setStatus("error", "Simulation failed");
  } finally {
    $("#btn-run").disabled = false;
    $("#btn-run").classList.remove("running");
    $("#progress").classList.add("hidden");
  }
}

function resetView() {
  stopPlay();
  S.sim = null;
  S.assetState = new Map();
  S.stageCanvases = [];
  S.stageIdx = 0;
  S.selectedId = null;
  if (S.hazardOverlay) { map.removeLayer(S.hazardOverlay); S.hazardOverlay = null; }
  $("#badge-hazard").classList.add("active");
  if (S.cascadeLayer) { map.removeLayer(S.cascadeLayer); S.cascadeLayer = null; }
  $("#legend").classList.add("hidden");
  $("#stage-scrubber").classList.add("hidden");
  $("#inspector").classList.add("hidden");
  $("#badge-cascade").classList.remove("active");
  S.showCascade = false;
  refreshStyles();
  renderSummary(null);
  setStatus("idle", "Ready");
}

/* ------------------------------------------------------------------ */
/* simulation rendering                                                */
/* ------------------------------------------------------------------ */
async function loadSimulation(sim) {
  S.sim = sim;
  S.assetState = new Map(sim.assets.map((a) => [a.asset_id, a]));
  S.stageIdx = 0;
  setStatus("done", `${sim.hazard_type} · ${sim.elapsed_s}s engine`);

  // decode + colorize all stage grids up-front (engine PNG -> canvas ramp)
  S.stageCanvases = [];
  for (const st of sim.stages) {
    S.stageCanvases.push(st.png ? await colorizeGrid(st.png, sim.hazard_type) : null);
  }

  showStage(0, true);
  renderSummary(sim);
  renderRanking(sim);
  buildCascadeLayer();
  $("#badge-cascade").disabled = false;
  $("#btn-play").disabled = sim.stages.length < 2;

  // scrubber for multi-stage hazards (flood propagation)
  const scrub = $("#stage-scrubber");
  if (sim.stages.length > 1) {
    scrub.classList.remove("hidden");
    const range = $("#scrub-range");
    range.max = sim.stages.length - 1;
    range.value = 0;
    $("#scrub-label").textContent = `1/${sim.stages.length}`;
  } else {
    scrub.classList.add("hidden");
  }
  map.flyToBounds(hazardBounds(sim), { padding: [24, 24], duration: 0.8 });
}

function hazardBounds(sim) {
  const [a, b, c, d, e, f] = sim.grid.transform;
  const h = sim.grid.height, w = sim.grid.width;
  // corners from the affine (GDAL order: x = c + col*a, y = f + row*e)
  return L.latLngBounds([[f + h * e, c], [f, c + w * a]]);
}

/* stage display ---------------------------------------------------- */
function showStage(idx, force = false) {
  const sim = S.sim;
  if (!sim) return;
  S.stageIdx = idx;

  // hazard overlay
  const url = S.stageCanvases[idx];
  if (url) {
    const bounds = hazardBounds(sim);
    if (S.hazardOverlay) S.hazardOverlay.setUrl(url);
    else S.hazardOverlay = L.imageOverlay(url, bounds, { opacity: 0.78, interactive: false }).addTo(map);
    S.hazardOverlay.setBounds(bounds);
  }
  refreshStyles(sim.stages.length > 1 ? idx : null);

  if (sim.stages.length > 1) {
    $("#scrub-range").value = idx;
    const label = sim.stage_labels?.[idx];
    $("#scrub-label").textContent = `${idx + 1}/${sim.stages.length}${label ? ` · ${label}` : ""}`;
  }
  renderLegend();
}

/* decode a 16-bit greyscale PNG to a colored data URL via canvas ------- */
const RAMPS = {
  flood: [[0, [8, 47, 73], 0.0], [0.25, [18, 90, 150], 0.45], [0.55, [34, 160, 220], 0.7], [0.85, [140, 215, 250], 0.9], [1, [235, 250, 255], 0.95]],
  heat: [[0, [120, 53, 15], 0.0], [0.3, [217, 119, 6], 0.4], [0.6, [234, 88, 12], 0.65], [0.85, [220, 38, 38], 0.85], [1, [254, 240, 138], 0.95]],
  cyclone: [[0, [59, 7, 100], 0.0], [0.35, [124, 58, 237], 0.4], [0.65, [219, 39, 119], 0.7], [1, [253, 224, 71], 0.9]],
};

function rampColor(ramp, v) {
  for (let i = ramp.length - 1; i >= 0; i--) {
    if (v >= ramp[i][0]) {
      const [p0, c0] = ramp[Math.max(i - 1, 0)];
      const [, c1, a1] = ramp[i];
      const t = ramp[i][0] === 0 ? 1 : (v - p0) / (ramp[i][0] - p0);
      const mix = ramp[i][0] === 0 ? c1 : c0.map((c, k) => Math.round(c + (c1[k] - c) * Math.min(1, Math.max(0, t))));
      return [...mix, (a1 * 255) | 0];
    }
  }
  return [0, 0, 0, 0];
}

function colorizeGrid(pngB64, hazard) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas");
      cv.width = img.width; cv.height = img.height;
      const ctx = cv.getContext("2d");
      ctx.drawImage(img, 0, 0);
      const data = ctx.getImageData(0, 0, cv.width, cv.height);
      const px = data.data;
      const ramp = RAMPS[hazard] || RAMPS.flood;
      for (let i = 0; i < px.length; i += 4) {
        const v = px[i] / 255; // 16-bit source collapses to 8-bit high byte
        if (v === 0) { px[i + 3] = 0; continue; }
        const [r, g, b, a] = rampColor(ramp, v);
        px[i] = r; px[i + 1] = g; px[i + 2] = b; px[i + 3] = a;
      }
      ctx.putImageData(data, 0, 0);
      resolve(cv.toDataURL("image/png"));
    };
    img.src = `data:image/png;base64,${pngB64}`;
  });
}

/* animation ---------------------------------------------------------- */
function playStages() {
  if (!S.sim || S.sim.stages.length < 2) return;
  if (S.playing) return stopPlay();
  S.playing = true;
  $("#scrub-play").textContent = "❚❚";
  let idx = S.stageIdx >= S.sim.stages.length - 1 ? 0 : S.stageIdx;
  S.playTimer = setInterval(() => {
    showStage(idx);
    idx += 1;
    if (idx >= S.sim.stages.length) { idx = 0; } // loop the propagation
  }, 1100);
}

function stopPlay() {
  S.playing = false;
  if (S.playTimer) clearInterval(S.playTimer);
  const b = $("#scrub-play");
  if (b) b.textContent = "▶";
}

$("#scrub-play").onclick = playStages;
$("#scrub-range").addEventListener("input", (e) => { stopPlay(); showStage(+e.target.value); });
$("#btn-play").onclick = () => { if (S.sim?.stages.length > 1) playStages(); };

/* ------------------------------------------------------------------ */
/* cascade edges                                                       */
/* ------------------------------------------------------------------ */
function buildCascadeLayer() {
  if (S.cascadeLayer) map.removeLayer(S.cascadeLayer);
  S.cascadeLayer = L.layerGroup();
  const sim = S.sim;
  if (!sim?.cascade_edges?.length) return;
  const topIds = new Set(sim.assets.slice(0, 60).map((a) => a.asset_id));
  const drawn = new Set();
  for (const e of sim.cascade_edges) {
    const key = `${e.source}|${e.target}`;
    if (drawn.has(key)) continue;
    drawn.add(key);
    const from = S.coordIndex[e.source] || coordFromAssets(e.source);
    const to = S.coordIndex[e.target] || coordFromAssets(e.target);
    if (!from || !to) continue;
    const highlight = topIds.has(e.source) || topIds.has(e.target);
    L.polyline([from, to], {
      color: highlight ? "#fb923c" : "#64748b",
      weight: highlight ? 1.8 : 0.8,
      opacity: highlight ? 0.85 : 0.35,
      dashArray: "4 5",
      className: "cascade-line",
    }).bindTooltip(`${e.dep_type} · w=${e.weight}`, { sticky: true })
      .addTo(S.cascadeLayer);
  }
  if (S.showCascade) S.cascadeLayer.addTo(map);
}

function coordFromAssets(id) {
  const a = S.assetState.get(id);
  return a ? [a.lat, a.lon] : null;
}

for (const btn of document.querySelectorAll(".basemap-btn")) {
  btn.onclick = () => setBasemap(btn.dataset.basemap);
}

$("#badge-hazard").onclick = () => {
  if (!S.hazardOverlay) return;
  const on = map.hasLayer(S.hazardOverlay);
  if (on) map.removeLayer(S.hazardOverlay); else S.hazardOverlay.addTo(map);
  $("#badge-hazard").classList.toggle("active", !on);
};

$("#badge-cascade").onclick = () => {
  S.showCascade = !S.showCascade;
  $("#badge-cascade").classList.toggle("active", S.showCascade);
  if (!S.sim) return;
  if (S.showCascade) { buildCascadeLayer(); S.cascadeLayer.addTo(map); }
  else if (S.cascadeLayer) map.removeLayer(S.cascadeLayer);
};

/* ------------------------------------------------------------------ */
/* panels: summary, ranking, legend                                    */
/* ------------------------------------------------------------------ */
function renderSummary(sim) {
  const cards = $("#summary-cards");
  const bars = $("#group-bars");
  const net = $("#network-stats");
  if (!sim) {
    cards.innerHTML = `<div class="empty">Run a simulation to populate results. All numbers come from the DEM-driven hazard engine — nothing here is scripted.</div>`;
    bars.innerHTML = ""; net.classList.add("hidden");
    return;
  }
  const s = sim.summary;
  cards.innerHTML = `
    <div class="card warn"><div class="v">${fmt.int(s.assets_exposed)}</div><div class="k">exposed assets</div></div>
    <div class="card bad"><div class="v">${fmt.int(s.assets_high_risk)}</div><div class="k">high risk (≥0.45)</div></div>
    <div class="card"><div class="v">${fmt.int(s.assets_high_impact)}</div><div class="k">high impact (≥0.5)</div></div>
    <div class="card"><div class="v">${fmt.pop(s.affected_population)}</div><div class="k">pop. affected</div></div>`;

  const groups = Object.entries(s.by_group || {}).filter(([, v]) => v.exposed > 0)
    .sort((a, b) => b[1].exposed - a[1].exposed);
  const maxExp = Math.max(1, ...groups.map(([, v]) => v.exposed));
  bars.innerHTML = groups.map(([g, v]) => `
    <div class="gbar" title="${g}">
      <span class="lbl">${(S.meta.groups[g] || g).toLowerCase()}</span>
      <span class="track"><span class="fill" style="width:${(v.exposed / maxExp * 100).toFixed(0)}%"></span></span>
      <span class="n">${v.exposed}</span>
    </div>`).join("");

  const n = s.network || {};
  if (Object.keys(n).length) {
    net.classList.remove("hidden");
    net.innerHTML = `
      <div><span>Road graph</span><b>${fmt.int(n.road_nodes)}n / ${fmt.int(n.road_edges)}e</b></div>
      <div><span>Failed road edges</span><b>${fmt.int(n.failed_edges)}</b></div>
      <div><span>Lost connectivity</span><b>${fmt.pct(n.lost_connectivity_fraction)}</b></div>
      <div><span>Max detour</span><b>${n.max_detour_min ?? 0} min</b></div>
      ${n.accessibility_increase_min !== undefined ? `<div><span>Access to hospitals +</span><b>${n.accessibility_increase_min} min</b></div>` : ""}`;
  } else net.classList.add("hidden");
}

function renderRanking(sim) {
  const list = $("#rank-list");
  if (!sim) { list.innerHTML = `<div class="empty">No simulation yet.</div>`; return; }
  const top = sim.assets.slice(0, 12);
  list.innerHTML = "";
  top.forEach((a, i) => {
    const li = document.createElement("li");
    li.className = "rank-item";
    li.innerHTML = `
      <span class="rk">${String(i + 1).padStart(2, "0")}</span>
      <span class="nm">${a.name || a.asset_id}<small>${(S.meta.groups[a.group] || a.group).toLowerCase()} · risk ${fmt.score(a.risk)} · impact ${fmt.score(a.impact)}</small></span>
      <span class="sc" style="color:${RISK_COLOR(a.risk)}">${fmt.score(a.priority)}</span>`;
    li.onclick = () => focusAsset(a.asset_id);
    list.appendChild(li);
  });
}

function renderLegend() {
  const el = $("#map-legend");
  if (!el) return;
  const sim = S.sim;
  const z = map.getZoom();
  const activePoints = Object.entries(S.pointZoomBound)
    .filter(([, min]) => z >= min)
    .map(([g]) => g);

  let html = "";

  // symbol legend: shown whenever any point layer is (or would be) visible
  if (activePoints.length) {
    const kindsShown = new Set();
    for (const g of activePoints) {
      for (const f of S.plainGeoms[g]?.features || []) {
        if (f.geometry?.type !== "Point") continue;
        const k = pointKind(g, safeTags(f.properties?.tags));
        if (k) kindsShown.add(k);
      }
    }
    if (kindsShown.size) {
      html += `<h3>Infrastructure</h3>`;
      for (const k of [...kindsShown].sort()) {
        const s = SYMBOLS[k];
        html += `<div class="row"><svg viewBox="0 0 24 24" width="16" height="16">${s.shape}</svg><span>${s.label}</span></div>`;
      }
      html += `<div class="row" style="color:var(--muted)"><span class="cluster-swatch"></span><span>count = clustered markers</span></div>`;
    }
  }

  // hazard legend (after a run)
  if (sim) {
    const stage = sim.stages[Math.min(S.stageIdx, sim.stages.length - 1)];
    const grad = {
      flood: "linear-gradient(90deg,#122d4a,#1e5a96,#22a0dc,#ecdff5)",
      heat: "linear-gradient(90deg,#78350f,#d97706,#ea580c,#dc2626,#fef07a)",
      cyclone: "linear-gradient(90deg,#3b0764,#7c3aed,#db2777,#fde047)",
    }[sim.hazard_type];
    const unit = { flood: "water depth", heat: "heat index", cyclone: "wind / surge" }[sim.hazard_type];
    html += `<h3>${sim.hazard_type} — ${unit}</h3>
      <div class="row"><span class="swatch" style="background:${grad}"></span><span>0 → ${stage?.meta?.max ?? 1}</span></div>
      <div class="row" style="color:var(--muted)">ring: red = failed · amber = exposed · orange = cascade</div>`;
  }

  if (!html) { el.innerHTML = ""; el.classList.add("hidden"); return; }
  el.innerHTML = html;
  el.classList.remove("hidden");
}

/* ------------------------------------------------------------------ */
/* asset inspector                                                     */
/* ------------------------------------------------------------------ */
function focusAsset(assetId) {
  const a = S.assetState.get(assetId);
  const coord = S.coordIndex[assetId] || (a ? [a.lat, a.lon] : null);
  openInspector(assetId);
  if (!coord) return;
  const targetZoom = Math.max(map.getZoom(), 16);
  map.flyTo(coord, targetZoom, { duration: 0.6 });
  // when the point layer is zoom-gated, ensure it is present after the fly
  const zl = targetZoom;
  for (const [group, cluster] of Object.entries(S.pointClusters)) {
    const min = S.pointZoomBound[group] ?? 12;
    const belongs = S.plainGeoms[group]?.features?.some((f) => featureId(f) === assetId
      && f.geometry?.type === "Point");
    if (belongs && zl >= min && !map.hasLayer(cluster)) {
      map.addLayer(cluster);
      S.pointZoomBound[group] = min; // gating re-applies on zoomend anyway
    }
  }
  // once clustered markers exist, spiderfy the cluster containing the asset
  setTimeout(() => {
    for (const cluster of Object.values(S.pointClusters)) {
      if (!map.hasLayer(cluster)) continue;
      let target = null;
      cluster.eachLayer((mk) => { if (mk.__assetId === assetId) target = mk; });
      if (target) cluster.zoomToShowLayer(target, () => target.fire("click"));
    }
  }, 700);
}

function selectAsset(assetId, lyr) {
  openInspector(assetId);
}

async function openInspector(assetId) {
  S.selectedId = assetId;
  const a = S.assetState.get(assetId);
  const panel = $("#inspector");
  panel.classList.remove("hidden");
  panel.setAttribute("aria-hidden", "false");

  const known = !!a;
  $("#insp-title").textContent = a?.name || assetId;

  const layer = Object.keys(S.layers).find((g) => {
    const gj = S.layers[g];
    return gj.features?.some((f) => f.id === assetId);
  });

  let html = "";
  if (!known) {
    html = `<div class="empty">This asset was not materially affected in the current scenario.</div>`;
  } else {
    const stateChip = a.failed === true ? `<span class="chip bad">failed</span>`
      : a.exposure > 0.05 ? `<span class="chip warn">exposed ${fmt.pct(a.exposure)}</span>`
      : `<span class="chip ok">affected via cascade</span>`;
    const meters = [
      ["Risk (E×V×C)", a.risk, "risk"],
      ["Impact if it fails", a.impact, "impact"],
      ["Priority", a.priority, "risk"],
      ["Exposure", a.exposure, "vuln"],
      ["Vulnerability", a.vulnerability, "vuln"],
      ["Criticality", a.criticality, "impact"],
      ["Network impact", a.network_impact, "impact"],
      ["Cascade impact", a.cascade_impact, "impact"],
    ];
    html = `
      <div class="chips">
        <span class="chip">${(S.meta.groups[a.group] || a.group).toLowerCase()}</span>
        <span class="chip">${a.category}</span>
        ${stateChip}
        ${a.impact_depth > 0 ? `<span class="chip warn">cascade depth ${a.impact_depth}</span>` : ""}
        ${a.dependents ? `<span class="chip">${a.dependents} dependents</span>` : ""}
      </div>
      <div class="kv">
        <div class="card"><div class="v" style="color:${RISK_COLOR(a.risk)}">${fmt.score(a.risk)}</div><div class="k">asset risk</div></div>
        <div class="card"><div class="v">${fmt.score(a.impact)}</div><div class="k">impact</div></div>
        ${a.depth_m != null ? `<div class="card"><div class="v">${a.depth_m} m</div><div class="k">water depth</div></div>` : ""}
        ${a.wind_ms != null ? `<div class="card"><div class="v">${a.wind_ms} m/s</div><div class="k">wind</div></div>` : ""}
        ${a.temp_c != null ? `<div class="card"><div class="v">${a.temp_c} °C</div><div class="k">temp</div></div>` : ""}
        <div class="card"><div class="v">${fmt.int(a.affected_population)}</div><div class="k">pop. served</div></div>
      </div>
      ${meters.map(([k, v, cls]) => `
        <div class="meter"><div class="m-head"><span>${k}</span><b>${fmt.score(v)}</b></div>
        <div class="track"><div class="fill ${cls}" style="width:${Math.min(100, (v || 0) * 100).toFixed(0)}%"></div></div></div>`).join("")}
      ${a.stage_exposure?.length > 1 ? stageChart(a.stage_exposure) : ""}`;
  }

  html += `<div class="dep-sec" id="dep-sec"><h3>Dependencies</h3><div class="empty">loading…</div></div>`;
  $("#insp-body").innerHTML = html;

  // dependencies from the real graph
  try {
    const deps = await fetch(`/api/dependencies/${encodeURIComponent(assetId)}`).then((r) => r.json());
    const sec = $("#dep-sec");
    if (!sec) return;
    const item = (n, tag, w) => `
      <div class="dep-item" data-asset="${n.id}">
        <span>${n.name || n.id}</span><span class="t">${tag}${w ? ` · w=${w}` : ""}</span>
      </div>`;
    sec.innerHTML = `<h3>Dependencies</h3>
      ${deps.depends_on.length ? deps.depends_on.map((d) => item(d, "depends on " + d.dep_type, d.weight)).join("") : `<div class="empty">no upstream dependencies</div>`}
      ${deps.dependents.length ? `<h3 style="margin-top:10px">Dependents</h3>${deps.dependents.map((d) => item(d, d.dep_type, d.weight)).join("")}` : `<div class="empty">no downstream dependents</div>`}`;
    for (const el of sec.querySelectorAll(".dep-item")) {
      el.onclick = () => focusAsset(el.dataset.asset);
    }
  } catch { /* 404: not in dep graph */ }
}

function stageChart(values) {
  const w = 300, h = 56, n = values.length;
  const max = Math.max(0.05, ...values);
  const bw = w / n;
  const bars = values.map((v, i) => {
    const bh = (v / max) * (h - 8);
    return `<rect x="${(i * bw + 1).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" fill="${RISK_COLOR(v >= 0.4 ? 0.5 : v)}" rx="1.5"></rect>`;
  }).join("");
  return `<div class="stage-chart"><div class="m-head" style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:4px"><span>Exposure through stages</span></div><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${bars}</svg></div>`;
}

$("#insp-close").onclick = () => {
  $("#inspector").classList.add("hidden");
  $("#inspector").setAttribute("aria-hidden", "true");
};

/* ------------------------------------------------------------------ */
/* misc UI                                                             */
/* ------------------------------------------------------------------ */
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 6000);
}

$("#btn-run").onclick = runSimulation;
$("#btn-reset").onclick = resetView;

boot();
