/* Ember Watch dashboard. Plain JS, no framework.
   Security: every value from data files is inserted with textContent (never innerHTML),
   data files are verified against manifest.json SHA-256 before use, and the API key
   (if any) lives only in memory for this tab. */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const ROUTE_LABEL = { priority_review: "Likely new fire", verify: "Needs a check", monitor_known: "Known fire", watch: "Weak signal", likely_static: "Gas flare / industrial" };
  const KIND = { observed: ["Observed", "Sensor and agency reports"], calculated: ["Calculated", "Deterministic analytics"], model: ["Model-derived", "Index or model output"], unknown: ["Unknown", "What the evidence cannot tell"] };
  const FILES = ["summary.json", "config.json", "risk.geojson", "risk_cells.json", "detections.geojson", "candidates.json", "incidents.geojson", "boundary.geojson", "places.geojson"];
  const meta = (n) => (document.querySelector(`meta[name="${n}"]`) || {}).content || "";
  const API = meta("wfi-api").replace(/\/$/, "");
  const BASEMAP = meta("wfi-basemap") === "carto";
  const EMBED = document.getElementById("embedded-data");
  const S = { jid: null, d: null, lead: 0, ageMax: 48, show: { risk: true, det: true, inc: true, places: true }, sel: null, cell: null, apiKey: null, markers: [], labels: [] };
  let map, popup;

  // ---------- tiny DOM builder (text only) ----------
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "style") Object.assign(n.style, v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  const clear = (n) => { while (n.firstChild) n.firstChild.remove(); return n; };
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  // ---------- units (FR-31) ----------
  function units(sys) {
    const imp = sys === "imperial";
    return {
      dist: (km) => km == null ? "–" : imp ? `${(km * 0.621371).toFixed(km < 16 ? 1 : 0)} mi` : `${km.toFixed(km < 10 ? 1 : 0)} km`,
      area: (km2) => imp ? [Math.round(km2 * 0.386102).toLocaleString(), "mi²"] : [Math.round(km2).toLocaleString(), "km²"],
      speed: (k) => k == null ? "–" : imp ? `${Math.round(k / 1.609344)} mph` : `${Math.round(k)} km/h`,
      temp: (c) => c == null ? "–" : imp ? `${Math.round(c * 9 / 5 + 32)} °F` : `${Math.round(c)} °C`,
    };
  }
  let U = units("metric");
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const compass = (d) => COMPASS[Math.round(((d % 360) + 360) % 360 / 22.5) % 16];
  const fmtTime = (iso, tz) => { try { return new Date(iso).toLocaleString(undefined, { timeZone: tz, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }); } catch { return iso; } };
  const ago = (iso) => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 1 ? "just now" : m < 90 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };

  // ---------- data loading with integrity check ----------
  async function sha256(buf) {
    const h = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function loadJurisdictions() {
    if (EMBED) return JSON.parse(EMBED.textContent).jurisdictions;
    const r = await fetch("data/jurisdictions.json", { cache: "no-cache" });
    if (!r.ok) throw new Error("No published data yet. Run the pipeline workflow once.");
    return r.json();
  }
  async function loadPack(jid) {
    if (!/^[a-z0-9][a-z0-9_-]{0,40}$/.test(jid)) throw new Error("bad jurisdiction id");
    if (EMBED) return JSON.parse(EMBED.textContent).packs[jid];
    const base = `data/${jid}/`;
    const man = await (await fetch(base + "manifest.json", { cache: "no-cache" })).json();
    const out = {};
    await Promise.all(FILES.map(async (f) => {
      const buf = await (await fetch(base + f, { cache: "no-cache" })).arrayBuffer();
      if (crypto.subtle && (await sha256(buf)) !== man.files[f]) throw new Error(`Integrity check failed for ${f}. Data was not displayed.`);
      out[f.split(".")[0]] = JSON.parse(new TextDecoder().decode(buf));
    }));
    return out;
  }

  // ---------- colours ----------
  function classColors(n) {
    const base = ["--r0", "--r1", "--r2", "--r3", "--r4"].map(css);
    if (n === 5) return base;
    return Array.from({ length: n }, (_, i) => base[Math.round(i * 4 / Math.max(1, n - 1))]);
  }

  // ---------- map ----------
  function initMap() {
    if (window.__WFI_WORKER__ && maplibregl.setWorkerUrl) maplibregl.setWorkerUrl(window.__WFI_WORKER__);
    const style = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": css("--map-bg") } }] };
    map = new maplibregl.Map({ container: "map", style, center: [-99, 31], zoom: 4.6, attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: "260px", offset: 8 });
    return new Promise((res) => map.on("load", res));
  }
  function setupLayers() {
    const empty = { type: "FeatureCollection", features: [] };
    if (BASEMAP) {
      const dark = matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light" || document.documentElement.dataset.theme === "dark";
      map.addSource("basemap", { type: "raster", tileSize: 256, attribution: "© OpenStreetMap contributors © CARTO",
        tiles: ["a", "b", "c"].map((s) => `https://${s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}.png`) });
      map.addLayer({ id: "basemap", type: "raster", source: "basemap", paint: { "raster-opacity": 0.9 } });
    }
    for (const id of ["boundary", "risk", "detections", "incidents", "places"]) map.addSource(id, { type: "geojson", data: empty });
    map.addLayer({ id: "land", type: "fill", source: "boundary", paint: { "fill-color": css("--map-land"), "fill-opacity": BASEMAP ? 0 : 1 } });
    map.addLayer({ id: "risk", type: "fill", source: "risk", paint: { "fill-opacity": 0.55, "fill-antialias": false } });
    map.addLayer({ id: "risk-sel", type: "line", source: "risk", paint: { "line-color": css("--fg"), "line-width": 2 }, filter: ["==", ["get", "id"], ""] });
    map.addLayer({ id: "border", type: "line", source: "boundary", paint: { "line-color": css("--map-line"), "line-width": 1.4 } });
    map.addLayer({ id: "perims", type: "fill", source: "incidents", filter: ["==", ["get", "role"], "perimeter"], paint: { "fill-color": css("--info"), "fill-opacity": 0.25 } });
    map.addLayer({ id: "perims-line", type: "line", source: "incidents", filter: ["==", ["get", "role"], "perimeter"], paint: { "line-color": css("--info"), "line-width": 1.6 } });
    map.addLayer({ id: "places", type: "circle", source: "places", paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2, 9, 4], "circle-color": css("--fg"), "circle-opacity": 0.65 } });
    map.addLayer({ id: "det", type: "circle", source: "detections", paint: {
      "circle-radius": ["interpolate", ["exponential", 2], ["zoom"], 4, 2.5, 8, 4, 12, ["*", ["get", "res_m"], 0.04]],
      "circle-color": ["interpolate", ["linear"], ["get", "age_h"], 0, css("--det-new"), 24, css("--det-old")],
      "circle-stroke-color": css("--det-ring"), "circle-stroke-width": 0.8, "circle-opacity": ["case", ["<", ["get", "confidence"], 0.5], 0.55, 0.95] } });
    map.addLayer({ id: "inc-pt", type: "circle", source: "incidents", filter: ["==", ["get", "role"], "point"], paint: { "circle-radius": 6, "circle-color": css("--info"), "circle-stroke-color": css("--surface"), "circle-stroke-width": 2 } });
    map.on("click", "risk", (e) => { const id = e.features[0].properties.id; selectCell(id); });
    map.on("mouseenter", "risk", () => (map.getCanvas().style.cursor = "crosshair"));
    map.on("mouseleave", "risk", () => (map.getCanvas().style.cursor = ""));
    map.on("mousemove", "det", (e) => {
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setDOMContent(el("div", null,
        el("b", { text: `${p.sensor} · ${p.platform}` }), el("br"),
        `${fmtTime(p.time, S.d.summary.jurisdiction.timezone)} (${p.age_h} h ago)`, el("br"),
        `Confidence ${p.confidence_raw} · FRP ${p.frp ?? "–"} MW`, el("br"),
        el("span", { class: "muted", text: `${p.dataset} · ${p.res_m} m pixel` }))).addTo(map);
    });
    map.on("mouseleave", "det", () => popup.remove());
    map.on("mousemove", "inc-pt", (e) => {
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setDOMContent(el("div", null, el("b", { text: p.name }), el("br"),
        `${p.size_acres != null ? Math.round(p.size_acres).toLocaleString() + " acres" : "size n/a"} · ${p.percent_contained ?? "–"}% contained`, el("br"),
        el("span", { class: "muted", text: `WFIGS ${p.id}` }))).addTo(map);
    });
    map.on("mouseleave", "inc-pt", () => popup.remove());
  }
  function applyColors() {
    if (!map || !S.d) return;
    const cols = classColors(S.d.config.risk.classes.length);
    const expr = ["match", ["get", `c${S.lead}`]];
    cols.forEach((c, i) => expr.push(i, c));
    expr.push("rgba(0,0,0,0)");
    map.setPaintProperty("risk", "fill-color", expr);
    map.setPaintProperty("bg", "background-color", css("--map-bg"));
    map.setPaintProperty("land", "fill-color", css("--map-land"));
    map.setPaintProperty("border", "line-color", css("--map-line"));
    map.setPaintProperty("det", "circle-stroke-color", css("--det-ring"));
    map.setPaintProperty("places", "circle-color", css("--fg"));
  }
  function applyVisibility() {
    const v = (on) => (on ? "visible" : "none");
    map.setLayoutProperty("risk", "visibility", v(S.show.risk));
    map.setLayoutProperty("det", "visibility", v(S.show.det));
    for (const l of ["perims", "perims-line", "inc-pt"]) map.setLayoutProperty(l, "visibility", v(S.show.inc));
    map.setLayoutProperty("places", "visibility", v(S.show.places));
    S.labels.forEach((m) => (m.getElement().style.display = S.show.places ? "" : "none"));
    map.setFilter("det", ["<=", ["get", "age_h"], S.ageMax]);
  }
  function loadMapData() {
    const d = S.d;
    map.getSource("boundary").setData(d.boundary);
    map.getSource("risk").setData(d.risk);
    map.getSource("detections").setData(d.detections);
    map.getSource("incidents").setData(d.incidents);
    map.getSource("places").setData(d.places);
    S.markers.forEach((m) => m.remove()); S.labels.forEach((m) => m.remove());
    S.markers = d.candidates.map((c, i) => {
      const b = el("button", { class: `cand-pin ${c.route}`, "aria-label": `${ROUTE_LABEL[c.route]} candidate ${c.id}`, title: `${c.id} · ${ROUTE_LABEL[c.route]}`, text: c.route === "likely_static" ? "" : String(i + 1),
        onclick: (e) => { e.stopPropagation(); select(c.id, true); } });
      b.dataset.id = c.id;
      return new maplibregl.Marker({ element: b }).setLngLat([c.lon, c.lat]).addTo(map);
    });
    const pl = [...d.places.features].sort((a, b) => b.properties.population - a.properties.population).slice(0, 14);
    S.labels = pl.map((f) => new maplibregl.Marker({ element: el("div", { class: "place-lbl", text: f.properties.name }), anchor: "left" }).setLngLat(f.geometry.coordinates).addTo(map));
    const b = d.config && bbox(d.boundary);
    if (b) map.fitBounds(b, { padding: 24, duration: 0 });
    applyColors(); applyVisibility();
  }
  function bbox(fc) {
    let x0 = 180, y0 = 90, x1 = -180, y1 = -90;
    for (const f of fc.features) for (const [x, y] of f.geometry.coordinates[0]) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
    return [[x0, y0], [x1, y1]];
  }

  // ---------- render: header, brief, KPIs ----------
  function renderHeader(js) {
    const sel = clear($("#jur"));
    js.forEach((j) => sel.append(el("option", { value: j.id, text: j.name, selected: j.id === S.jid })));
    const s = S.d.summary;
    const mode = $("#mode");
    mode.className = `pill ${s.run.mode === "live" ? "live" : s.run.mode === "replay" ? "" : "sample"}`;
    mode.textContent = { live: "Live feeds", replay: "Historical replay · archived feeds" }[s.run.mode] || "Sample data · synthetic inputs";
    const fr = $("#fresh");
    const stale = s.run.mode === "live" && (Date.now() - new Date(s.generated_at)) > 90 * 60000;
    fr.className = `pill${stale ? " stale" : ""}`;
    clear(fr).append(el("span", { class: "dot" }), s.run.mode === "replay" ? `As of ${fmtTime(s.generated_at, s.jurisdiction.timezone)}` : EMBED ? `Sample run ${fmtTime(s.generated_at, s.jurisdiction.timezone)}` : `Updated ${ago(s.generated_at)}`);
    fr.title = fmtTime(s.generated_at, s.jurisdiction.timezone);
  }
  function renderBrief() {
    const s = S.d.summary, ul = clear($("#brief-list"));
    const llm = s.brief.llm;
    (s.brief.deterministic.length ? s.brief.deterministic : ["No active detections or elevated alerts."]).forEach((t) => ul.append(el("li", { text: t })));
    if (llm && llm.headline) ul.append(el("li", { class: "muted", text: `AI draft (${llm.generated_by}, ${llm.status}): ${llm.headline}` }));
  }
  function renderKpis() {
    const k = S.d.summary.kpis, r = k.candidates_by_route || {};
    const area = U.area(k.area_high_or_above_km2[S.lead] ?? 0);
    const sensors = Object.entries(k.detections_48h_by_sensor || {}).map(([a, b]) => `${a} ${b}`).join(" · ") || "none";
    const box = clear($("#kpis"));
    const kpi = (cls, label, v, small, d) => box.append(el("div", { class: `kpi ${cls}` }, el("span", { class: "lbl", text: label }), el("span", { class: "v" }, String(v), small ? el("small", { text: small }) : null), el("span", { class: "d", text: d })));
    kpi(r.priority_review ? "alert" : "", "Likely new fires", r.priority_review || 0, "", "seen twice, no known fire there");
    kpi(r.verify ? "warn" : "", "Need a check", r.verify || 0, "", "send a camera, drone or crew");
    kpi("", "Known fires", k.known_incidents, "", `${r.monitor_known || 0} also seen by satellite`);
    kpi("", "Satellite heat spots", k.detections_48h, "", `last 48 h · ${sensors}`);
    kpi("", `Dangerous fire weather · ${["today", "tomorrow", "day 3"][S.lead]}`, area[0], area[1], "area rated High or worse");
    kpi("", "Worst fire-weather score", k.peak_fwi_today ?? "–", "", S.d.config.risk.validation.status === "validated" ? "FWI · validated here" : "FWI · above 38 is extreme · not yet tuned here");
  }

  // ---------- queue + detail ----------
  function renderQueue() {
    const q = clear($("#queue"));
    const cs = S.d.candidates;
    $("#queue-count").textContent = `${cs.length} cluster${cs.length === 1 ? "" : "s"}`;
    if (!cs.length) { q.append(el("li", { class: "empty", text: "No thermal detections in the current window." })); return; }
    cs.forEach((c, i) => {
      const placeTxt = c.incident ? `Matches ${c.incident.name}` : c.exposure.find((e) => e.downwind) ? `${c.exposure.find((e) => e.downwind).name} downwind, ${U.dist(c.exposure.find((e) => e.downwind).distance_km)}` : c.exposure[0] ? `${U.dist(c.exposure[0].distance_km)} from ${c.exposure[0].name}` : "No populated place nearby";
      q.append(el("li", null, el("button", { class: `qitem r-${c.route}`, "aria-current": S.sel === c.id ? "true" : "false", onclick: () => select(c.id, true) },
        el("span", { class: "qhead" }, el("span", { class: "chip", text: `${i + 1} · ${ROUTE_LABEL[c.route]}` }), el("span", { class: "qmeta", text: `seen ${ago(c.last_seen)}` })),
        el("span", { class: "qtitle", text: placeTxt }),
        el("span", { class: "qmeta", text: `${c.id} · ${c.components.observations} obs · ${c.components.passes} pass${c.components.passes > 1 ? "es" : ""} · ${c.components.platforms.join(", ")}` }),
        el("span", { class: "conf" }, el("span", { text: "Score" }), el("span", { class: "bar" }, el("i", { style: { width: `${Math.round(c.score * 100)}%` } })), el("span", { class: "mono", text: c.score.toFixed(2) })))));
    });
  }
  function select(id, fly) {
    S.sel = id;
    const c = S.d.candidates.find((x) => x.id === id);
    document.querySelectorAll(".cand-pin").forEach((p) => p.classList.toggle("sel", p.dataset.id === id));
    renderQueue(); renderDetail();
    if (c && fly) map.flyTo({ center: [c.lon, c.lat], zoom: Math.max(map.getZoom(), 9), duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 900 });
    if (c && c.weather) selectCell(c.weather.cell_id, false);
  }
  function renderDetail() {
    const box = clear($("#detail"));
    const c = S.d.candidates.find((x) => x.id === S.sel);
    if (!c) { box.append(el("p", { class: "empty", text: "Select a cluster in the queue or on the map to see its evidence." })); return; }
    const tz = S.d.summary.jurisdiction.timezone;
    box.append(el("div", { class: "cardhead" },
      el("div", null, el("h2", { text: `${c.id} · ${ROUTE_LABEL[c.route]}` }), el("p", { class: "sub mono", text: `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)} ± ${U.dist(c.location_uncertainty_km)} · first ${fmtTime(c.first_seen, tz)} · last ${fmtTime(c.last_seen, tz)}` })),
      el("span", { class: `chip r-${c.route}`, style: { fontSize: "12px" }, text: `score ${c.score.toFixed(2)}` })));
    box.append(el("p", { class: "status", text: c.recommendation }));
    const ev = el("div", { class: "ev" });
    for (const k of ["observed", "calculated", "model", "unknown"]) {
      const items = c.explanation.filter((e) => e.kind === k);
      if (!items.length) continue;
      ev.append(el("div", { class: "evgroup" }, el("h3", null, KIND[k][0], el("span", { class: "tag", text: KIND[k][1] })),
        el("ul", null, items.map((s) => el("li", null, s.text, s.evidence.length ? el("span", { class: "refs" }, s.evidence.slice(0, 3).map((r) => el("code", { text: r })), s.evidence.length > 3 ? el("code", { text: `+${s.evidence.length - 3}` }) : null) : null)))));
    }
    const right = el("div", { class: "ev" });
    const comp = c.components;
    right.append(el("div", null, el("span", { class: "lbl", text: "Confidence components" }), el("dl", { class: "facts" },
      el("dt", { text: "Detection evidence" }), el("dd", { text: comp.detection_evidence.toFixed(3) }),
      el("dt", { text: "Weather factor" }), el("dd", { text: String(comp.weather_factor) }),
      el("dt", { text: "Static-source factor" }), el("dd", { text: String(comp.static_factor) }),
      el("dt", { text: "Platforms" }), el("dd", { text: comp.platforms.join(", ") }),
      el("dt", { text: "FRP total / max" }), el("dd", { text: `${c.frp_total_mw} / ${c.frp_max_mw} MW` }),
      el("dt", { text: "Extent · trend" }), el("dd", { text: `${U.dist(comp.extent_km)} · ${comp.growth}` }))));
    right.append(el("div", { class: "tbl" }, el("span", { class: "lbl", text: "Satellite passes" }), el("table", null,
      el("thead", null, el("tr", null, el("th", { text: "Time" }), el("th", { text: "Platform" }), el("th", { text: "Pixels" }), el("th", { text: "p" }))),
      el("tbody", null, c.passes.map((p) => el("tr", null, el("td", { text: fmtTime(p.time, tz) }), el("td", { text: `${p.sensor} ${p.platform}` }), el("td", { class: "num", text: String(p.pixels) }), el("td", { class: "num", text: p.p.toFixed(2) })))))));
    if (c.exposure.length) right.append(el("div", { class: "tbl" }, el("span", { class: "lbl", text: "Exposure (populated places)" }), el("table", null,
      el("thead", null, el("tr", null, el("th", { text: "Place" }), el("th", { text: "Dist." }), el("th", { text: "Dir." }), el("th", { text: "Downwind" }))),
      el("tbody", null, c.exposure.map((e) => el("tr", null, el("td", { text: e.name }), el("td", { class: "num", text: U.dist(e.distance_km) }), el("td", { text: e.direction }), el("td", { text: e.downwind ? "Yes" : "No" })))))));
    if (c.verification_task) {
      const t = c.verification_task;
      right.append(el("div", { class: "task" }, el("b", { text: "Suggested verification task" }),
        el("span", { text: `Search radius ${U.dist(t.search_radius_km)} around ${c.lat.toFixed(4)}, ${c.lon.toFixed(4)}` }),
        el("span", { text: `Sensors: ${t.sensors.join(" + ")} · Aviation profile: ${t.aviation_profile}` }),
        el("span", { class: "muted", text: t.note })));
    }
    box.append(el("div", { class: "dgrid" }, ev, right));
    // analyst feedback (FR-29)
    const fb = el("div", { class: "fb" });
    const status = el("span", { class: "note", role: "status" });
    const send = async (decision) => {
      if (!API || !S.apiKey) return;
      status.textContent = "Recording…";
      try {
        const r = await fetch(`${API}/v1/${S.jid}/candidates/${c.id}/feedback`, { method: "POST", headers: { "Content-Type": "application/json", "X-API-Key": S.apiKey }, body: JSON.stringify({ decision }) });
        status.textContent = r.ok ? `Recorded: ${decision.replace("_", " ")} (audit ${(await r.json()).hash.slice(0, 10)}…)` : r.status === 403 ? "Your key does not have the analyst role." : r.status === 401 ? "API key rejected." : `Could not record (HTTP ${r.status}).`;
      } catch { status.textContent = "Evidence API unreachable. Nothing was recorded."; }
    };
    const can = !!(API && S.apiKey);
    fb.append(el("span", { class: "lbl", text: "Analyst decision" }),
      el("button", { class: "btn", disabled: !can, onclick: () => send("confirmed"), text: "Confirm fire" }),
      el("button", { class: "btn", disabled: !can, onclick: () => send("rejected"), text: "Reject" }),
      el("button", { class: "btn", disabled: !can, onclick: () => send("verification_requested"), text: "Request verification" }), status);
    status.textContent = can ? "Decisions are written to the tamper-evident audit log." : API ? "Enter an analyst API key above to record decisions." : "Recording decisions needs the Evidence API (see README). This page is read-only.";
    box.append(fb);
  }

  // ---------- fire-weather inspector (FR-7) ----------
  function selectCell(id, redraw = true) {
    S.cell = id;
    map.setFilter("risk-sel", ["==", ["get", "id"], id || ""]);
    if (redraw !== false) renderInspector(); else renderInspector();
  }
  function renderInspector() {
    const box = clear($("#inspector"));
    const cell = S.d.risk_cells[S.cell];
    box.append(el("div", { class: "cardhead" }, el("div", null, el("h2", { text: "Fire-weather drivers" }), el("p", { class: "sub", text: "Click any grid cell on the map. Every driver behind the class is shown." }))));
    if (!cell) { box.append(el("p", { class: "empty", text: "No cell selected." })); return; }
    const d = cell.days[Math.min(S.lead, cell.days.length - 1)];
    const cols = classColors(S.d.config.risk.classes.length);
    box.append(el("p", { class: "sub mono", text: `Cell ${cell.lat.toFixed(2)}, ${cell.lon.toFixed(2)} · ${S.d.config.grid.spacing_deg}° grid · ${d.date} · spun up over ${cell.spinup_days} days` }));
    box.append(el("div", { class: "bigrow" }, el("div", null, el("span", { class: "lbl", text: "Fire Weather Index" }), el("div", { class: "big", text: d.fwi.toFixed(1) })),
      el("span", { class: "classchip" }, el("i", { style: { background: cols[d.class_index] } }), d.class)));
    const bar = (name, v, max, label) => el("div", { class: "drv" }, el("span", { text: name }), el("span", { class: "track" }, el("i", { class: "fill", style: { width: `${Math.min(100, (v / max) * 100)}%` } })), el("span", { class: "val", text: label }));
    box.append(el("div", { class: "drivers" },
      bar("Fine fuel moisture (FFMC)", Math.max(0, d.ffmc - 60), 41, d.ffmc.toFixed(1)),
      bar("Duff moisture (DMC)", d.dmc, 80, d.dmc.toFixed(0)),
      bar("Drought code (DC)", d.dc, 600, d.dc.toFixed(0)),
      bar("Initial spread (ISI)", d.isi, 30, d.isi.toFixed(1)),
      bar("Buildup (BUI)", d.bui, 120, d.bui.toFixed(0)),
      bar("Fosberg FFWI (max)", d.ffwi_max, 100, d.ffwi_max.toFixed(0)),
      bar("Hot-Dry-Windy (sfc)", d.hdw_max, 600, d.hdw_max.toFixed(0)),
      bar("Min humidity", 100 - (d.rh_min ?? 100), 100, d.rh_min != null ? `${d.rh_min}%` : "–"),
      bar("Max gust", d.gust_max_kmh ?? 0, 100, U.speed(d.gust_max_kmh)),
      bar("Days without rain", d.dry_days, 40, `${d.dry_days} d`)));
    box.append(el("div", { class: "days" }, cell.days.map((x) => el("div", null, el("span", { class: "lbl", text: x.lead_days === 0 ? "Today" : x.lead_days === 1 ? "Tomorrow" : x.date }),
      el("b", { text: `FWI ${x.fwi.toFixed(1)}` }), el("span", { class: "muted", text: `${x.class} · RH ${x.rh_min ?? "–"}% · ${U.temp(x.t_max_c)} · wind ${U.speed(x.wind_noon_kmh)} ${compass(x.wind_dir_noon)}` })))));
  }

  // ---------- provenance + config ----------
  function renderSources() {
    const s = S.d.summary, tz = s.jurisdiction.timezone, box = clear($("#sources"));
    box.append(el("div", { class: "cardhead" }, el("div", null, el("h2", { text: "Sources and provenance" }), el("p", { class: "sub", text: `Run ${s.run.id} · ${s.run.duration_s}s · pipeline ${s.pipeline_version}${s.run.commit ? " · " + s.run.commit : ""}` }))));
    box.append(el("div", { class: "tbl" }, el("table", null,
      el("thead", null, el("tr", null, ["Provider / dataset", "Status", "Records", "Latest obs.", "Latency", "Resolution", "License"].map((t) => el("th", { text: t })))),
      el("tbody", null, s.sources.map((x) => el("tr", null,
        el("td", null, el("b", { text: x.provider }), el("br"), el("span", { class: "muted", text: x.dataset })),
        el("td", { class: `st-${x.status}`, text: x.status + (x.message ? ` — ${x.message}` : "") }),
        el("td", { class: "num", text: String(x.records) }),
        el("td", { text: x.latest_observation ? fmtTime(x.latest_observation, tz) : "–" }),
        el("td", { class: "num", text: x.latency_min != null ? `${x.latency_min} min` : "–" }),
        el("td", { text: x.native_resolution }), el("td", { class: "muted", text: x.license })))))));
    const m = s.model_card;
    box.append(el("div", { class: "modelcard" }, el("b", { text: `Model ${m.id}: ${m.validation_status.replace("_", " ")} for ${s.jurisdiction.name}` }), el("p", { text: m.method }), el("p", { class: "muted", text: m.note })));
  }
  function renderConfig() {
    const c = S.d.config, box = clear($("#config"));
    box.append(el("div", { class: "cardhead" }, el("div", null, el("h2", { text: `Jurisdiction pack: ${c.name}` }), el("p", { class: "sub", text: "Loaded from configuration. The core engine has no jurisdiction-specific code." }))));
    const pr = c.providers;
    const item = (k, v) => el("div", null, el("span", { class: "lbl", text: k }), el("span", { text: v }));
    box.append(el("div", { class: "cfg" },
      item("Agencies", Object.values(c.roles).join(" · ")),
      item("Units · time zone · languages", `${c.units} · ${c.timezone} · ${c.languages.join(", ")}`),
      item("Active fire", pr.active_fire.map((p) => `${p.id} (${p.sources.length} datasets)`).join(", ") || "none"),
      item("Weather model", pr.weather.map((p) => `${p.id}: ${p.model}`).join(", ") || "none"),
      item("Incidents", pr.incidents.map((p) => p.id).join(", ") || "none configured"),
      item("Routing thresholds", `review ≥ ${c.detection.routing.priority_review} · verify ≥ ${c.detection.routing.verify}`),
      item("Ecosystems", c.ecosystems.join(", ").replaceAll("_", " ")),
      item("Compliance · aviation", `${c.compliance.join(", ")} · ${c.aviation.regulation_profile}`)));
  }
  function renderLegend() {
    const cls = S.d.config.risk.classes, cols = classColors(cls.length), box = clear($("#legend"));
    box.append(el("span", { class: "lbl", text: "Squares: how easily fire spreads" }), el("span", { class: "ramp" }, cls.map((c, i) => el("span", null, el("i", { style: { background: cols[i] } }), `${c.name}`))));
    box.append(el("span", { class: "sym" }, el("i", { style: { background: css("--det-new") } }), "Satellite heat spot (new)"),
      el("span", { class: "sym" }, el("i", { style: { background: css("--det-old") } }), "24 h+ old"),
      el("span", { class: "sym" }, el("i", { style: { background: css("--info") } }), "Incident / perimeter"));
  }

  // ---------- wiring ----------
  async function show(jid, js) {
    S.jid = jid; S.sel = null; S.cell = null;
    S.d = await loadPack(jid);
    U = units(S.d.summary.jurisdiction.units);
    renderHeader(js); renderBrief(); renderKpis(); renderLegend(); renderQueue(); renderSources(); renderConfig();
    loadMapData();
    const first = S.d.candidates.find((c) => c.route !== "likely_static");
    if (first) select(first.id, false); else { renderDetail(); selectCell(S.d.summary.kpis.peak_cell); }
    if (!S.cell) selectCell(S.d.summary.kpis.peak_cell);
    try { history.replaceState(null, "", `#${jid}`); } catch {}
  }
  function wireGuide() {
    const g = $("#guide");
    if (!g) return;
    try { if (localStorage.getItem("wfi-guide") === "closed") g.open = false; } catch {}
    g.addEventListener("toggle", () => { try { localStorage.setItem("wfi-guide", g.open ? "open" : "closed"); } catch {} });
  }
  function wire(js) {
    wireGuide();
    $("#jur").addEventListener("change", (e) => show(e.target.value, js).catch(fail));
    document.querySelectorAll("#lead button").forEach((b) => b.addEventListener("click", () => {
      S.lead = +b.dataset.lead;
      document.querySelectorAll("#lead button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      applyColors(); renderKpis(); renderInspector();
    }));
    for (const k of ["risk", "det", "inc", "places"]) $(`#show-${k}`).addEventListener("change", (e) => { S.show[k] = e.target.checked; applyVisibility(); });
    $("#age").addEventListener("input", (e) => { S.ageMax = +e.target.value; $("#age-out").textContent = `${S.ageMax} h`; applyVisibility(); });
    const keyBox = $("#apikey");
    if (API) {
      keyBox.hidden = false;
      $("#apikey-btn").addEventListener("click", () => { S.apiKey = $("#apikey-in").value.trim() || null; $("#apikey-in").value = ""; $("#apikey-btn").textContent = S.apiKey ? "Key set" : "Use key"; renderDetail(); });
    }
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const recolor = () => { applyColors(); renderLegend(); renderInspector(); };
    mq.addEventListener("change", recolor);
    new MutationObserver(recolor).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    if (!EMBED) setInterval(async () => {
      try {
        const s = await (await fetch(`data/${S.jid}/summary.json`, { cache: "no-cache" })).json();
        if (s.generated_at !== S.d.summary.generated_at) show(S.jid, await loadJurisdictions());
        else renderHeader(js);
      } catch {}
    }, 5 * 60000);
  }
  function fail(e) {
    const b = $("#fatal"); b.hidden = false; b.textContent = e.message || String(e);
  }
  async function main() {
    const js = await loadJurisdictions();
    const want = (location.hash || "").slice(1);
    await initMap(); setupLayers(); wire(js);
    await show(js.some((j) => j.id === want) ? want : js[0].id, js);
  }
  main().catch(fail);
})();
