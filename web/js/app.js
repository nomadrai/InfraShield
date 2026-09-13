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

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  subdomains: "abcd", maxZoom: 19,
}).addTo(map);

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
async function boot() {
  try {
    const [meta, layers] = await Promise.all([
      fetch("/api/meta").then((r) => r.json()),
      fetch("/api/layers").then((r) => r.json()),
    ]);
    S.meta = meta;
    S.layers = layers;

    $("#study-name").textContent = `${meta.study_area.name} · DEM ${meta.study_area.elevation_range_m[0]}–${meta.study_area.elevation_range_m[1]} m · ${meta.study_area.grid.cell_size_m} m grid`;
    const counts = Object.entries(meta.counts).map(([k, v]) => `<b>${fmt.int(v)}</b> ${k.replace("_", " ")}`).join(" · ");
    $("#header-meta").innerHTML = counts;

    buildAssetLayers();
    buildHazardPicker();
  } catch (err) {
    toast(`Failed to load backend data: ${err.message}`);
    setStatus("error", "Backend unreachable");
  }
}

function buildAssetLayers() {
  for (const [group, gj] of Object.entries(S.layers)) {
    if (!GROUP_STYLE[group]) continue;
    const layer = L.geoJSON(gj, {
      style: () => styleFor(group, null),
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, styleFor(group, null)),
      onEachFeature: (feature, lyr) => {
        lyr.on("click", () => selectAsset(feature.id ?? feature.properties.osm_type + "/" + feature.properties.osm_id, lyr));
        lyr.on("mouseover", () => lyr.setStyle({ weight: (GROUP_STYLE[group]?.weight || 1.5) + 1.5, opacity: 1 }));
        lyr.on("mouseout", () => lyr.setStyle(styleFor(group, S.assetState.get(feature.id))));
        // remember coordinates for cascade lines / flyTo
        const c = feature.geometry?.coordinates;
        if (c) {
          const pt = Array.isArray(c[0]) ? c[Math.floor(c.length / 2)] : c;
          S.coordIndex[feature.id] = Array.isArray(pt[0]) ? [pt[0][1], pt[0][0]] : [pt[1], pt[0]];
        }
      },
    });
    S.assetLayers[group] = layer;
  }
  // add in draw order
  for (const g of GROUP_ORDER) if (S.assetLayers[g]) S.assetLayers[g].addTo(map);
}

/* ------------------------------------------------------------------ */
/* styling driven by simulation results                                */
/* ------------------------------------------------------------------ */
const RISK_COLOR = (r) =>
  r >= 0.6 ? "#ef4444" : r >= 0.4 ? "#f97316" : r >= 0.2 ? "#fbbf24" : "#fde68a";

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
  $("#severity-field").querySelector("label").childNodes[0].textContent = `${pSpec.label} `;
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
    $("#secondary-note").textContent = "Tip: click the map to move the landfall point (default offshore).";
  } else {
    $("#secondary-note").textContent = "";
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
  const params = S.meta.hazards[key].params;
  const keys = Object.keys(params);
  $("#severity-out").textContent = $("#severity").value + (keys[0] === "rainfall_mm" ? " mm" : keys[0] === "anomaly_c" ? " °C" : "");
  if (keys[1]) {
    $("#secondary").parentElement.querySelector("label").childNodes[0].textContent = `${params[keys[1]].label} `;
    $("#secondary-out")?.remove();
  }
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
  const sim = S.sim;
  if (!sim) { el.classList.add("hidden"); return; }
  const stage = sim.stages[Math.min(S.stageIdx, sim.stages.length - 1)];
  const grad = {
    flood: "linear-gradient(90deg,#122d4a,#1e5a96,#22a0dc,#ecdff5)",
    heat: "linear-gradient(90deg,#78350f,#d97706,#ea580c,#dc2626,#fef07a)",
    cyclone: "linear-gradient(90deg,#3b0764,#7c3aed,#db2777,#fde047)",
  }[sim.hazard_type];
  const unit = { flood: "water depth", heat: "heat index", cyclone: "wind / surge" }[sim.hazard_type];
  el.innerHTML = `<h3>${sim.hazard_type} — ${unit}</h3>
    <div class="row"><span class="swatch" style="background:${grad}"></span><span>0 → ${stage?.meta?.max ?? 1}</span></div>
    <div class="row" style="color:var(--muted)">red outline = failed · yellow = exposed</div>`;
  el.classList.remove("hidden");
}

/* ------------------------------------------------------------------ */
/* asset inspector                                                     */
/* ------------------------------------------------------------------ */
function focusAsset(assetId) {
  const a = S.assetState.get(assetId);
  const coord = S.coordIndex[assetId] || (a ? [a.lat, a.lon] : null);
  openInspector(assetId);
  if (coord) map.flyTo(coord, Math.max(map.getZoom(), 15), { duration: 0.6 });
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
