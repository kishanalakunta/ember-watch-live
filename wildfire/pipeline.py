"""End-to-end run: providers -> evidence -> analytics -> published data products.

    python -m wildfire.pipeline --jurisdiction texas --out site/data
    python -m wildfire.pipeline --all --out site/data --fixtures      # offline demo

Outputs (per jurisdiction, schema v1): summary.json, config.json, risk.geojson,
risk_cells.json, detections.geojson, candidates.json, incidents.geojson,
boundary.geojson, places.geojson, manifest.json (SHA-256 of every file).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, llm
from .analytics import detect, risk
from .config import list_packs, load_pack
from .explain import Units, explain_candidate
from .fixtures import firms_csv_rows, wfigs_fixture
from .geo import grid_in_ring
from .models import SourceMeta
from .providers import firms, geonames, open_meteo, wfigs

log = logging.getLogger("wildfire")
SCHEMA = "wildfire-intel/v1"
ACTIVE_FIRE = {"nasa_firms": firms.fetch}
INCIDENTS = {"nifc_wfigs": wfigs.fetch}
WEATHER = {"open_meteo": open_meteo.fetch}


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), default=str, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def _cell_polygon(lat, lon, half):
    return [[[round(lon - half, 4), round(lat - half, 4)], [round(lon + half, 4), round(lat - half, 4)],
             [round(lon + half, 4), round(lat + half, 4)], [round(lon - half, 4), round(lat + half, 4)],
             [round(lon - half, 4), round(lat - half, 4)]]]


def run(pack_id: str, out_root: Path, *, fixtures: bool, cache_dir: Path | None,
        as_of: datetime | None = None, label: str = "") -> dict:
    """One jurisdiction. `as_of` replays a past moment from archived feeds (PRD Screen 3)."""
    t0 = time.monotonic()
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    pack = load_pack(pack_id)
    out_id = f"{pack.id}-replay-{now:%Y%m%d}" if as_of else pack.id
    units = Units(pack.units)
    fx_dir = None
    if fixtures:
        fx_dir = Path(tempfile.mkdtemp(prefix=f"fx-{pack_id}-"))
        for name, text in firms_csv_rows(now, pack_id=pack_id).items():
            (fx_dir / f"firms_{name}.csv").write_text(text)
        inc, per = wfigs_fixture(now)
        (fx_dir / "wfigs_incidents.geojson").write_text(json.dumps(inc))
        (fx_dir / "wfigs_perimeters.geojson").write_text(json.dumps(per))

    sources = []
    observations, incidents = [], []

    def guarded(category: str, ref, call, empty):
        """One broken provider must never take down the whole run."""
        try:
            return call()
        except Exception as e:  # noqa: BLE001 — recorded in provenance, run continues
            log.exception("%s provider %s crashed", category, ref.id)
            sources.append(SourceMeta(provider=ref.id, dataset=category, status="failed", fetched_at=now,
                                      native_resolution="-", expected_refresh="-", license="-", attribution="-",
                                      source_uri="-", message=f"{type(e).__name__}: {str(e)[:200]}"))
            return empty

    for ref in pack.providers.active_fire:
        o, m = guarded("active_fire", ref, lambda ref=ref: ACTIVE_FIRE[ref.id](pack, ref, now=now, fixtures=fx_dir, as_of=as_of), ([], []))
        observations += [x for x in o if x.observation_time <= now]
        sources += m
    for ref in pack.providers.incidents if not as_of else []:
        i, m = guarded("incidents", ref, lambda ref=ref: INCIDENTS[ref.id](pack, ref, now=now, fixtures=fx_dir), ([], []))
        incidents += i
        sources += m
    spacing = pack.geography.grid_spacing_deg
    points = grid_in_ring(pack.boundary_ring, spacing)
    cells = []
    for ref in pack.providers.weather[:1]:
        series, m = guarded("weather", ref, lambda ref=ref: WEATHER[ref.id](pack, ref, points, now=now, cache_dir=cache_dir,
                                                                          fixtures=fx_dir, as_of=as_of), (None, []))
        sources += m
        cells = risk.compute(pack, points, series, now) if series else []
    if fx_dir:
        shutil.rmtree(fx_dir, ignore_errors=True)

    if not fixtures:   # finer exposure layer when the network allows (falls back to the pack's layer)
        fc, m = geonames.load(pack, cache_dir, now)
        sources.append(m)
        if fc and fc["features"]:
            pack = pack.model_copy(update={"places_geojson": fc})
    candidates = detect.build_candidates(observations, incidents, cells, pack, now)
    src_ref = {"incidents": next((f"{s.provider}" for s in sources if s.provider == "nifc_wfigs"), "no incident feed configured"),
               "weather": next((s.provider for s in sources if s.provider == "open_meteo"), "weather")}
    for c in candidates:
        c["explanation"] = explain_candidate(c, units, src_ref)

    # ---- KPIs -------------------------------------------------------------
    km_per_deg = 111.32
    def cell_area(lat):
        return (spacing * km_per_deg) * (spacing * km_per_deg * math.cos(math.radians(lat)))
    high_min = next((i for i, c in enumerate(pack.risk.classes) if c.name.lower() == "high"), 2)
    area_by_lead = []
    for lead in range(3):
        a = sum(cell_area(c["lat"]) for c in cells if len(c["days"]) > lead and c["days"][lead]["class_index"] >= high_min)
        area_by_lead.append(round(a))
    peak = max(cells, key=lambda c: c["days"][0]["fwi"], default=None)
    recent = [o for o in observations if o.observation_time >= now - timedelta(hours=48)]
    routes = Counter(c["route"] for c in candidates)
    class_hist = Counter(c["days"][0]["class"] for c in cells)

    # deterministic situation brief; LLM draft only if configured
    lines = []

    def n(k: int, one: str, many: str) -> str:
        return f"{k} {one if k == 1 else many}"
    if routes.get("priority_review"):
        k = routes["priority_review"]
        lines.append(f"{n(k, 'likely new fire needs', 'likely new fires need')} review: seen on more than one satellite look, with no known fire there.")
    if routes.get("verify"):
        k = routes["verify"]
        lines.append(f"{n(k, 'heat spot needs', 'heat spots need')} a check by camera, drone or crew before anyone acts.")
    if routes.get("monitor_known"):
        k = routes["monitor_known"]
        lines.append(f"{n(k, 'heat spot matches a fire', 'heat spots match fires')} the fire service already tracks.")
    if peak:
        lines.append(f"Worst fire weather today: {peak['days'][0]['class']} (score {peak['days'][0]['fwi']}) near "
                     f"{peak['lat']:.2f}, {peak['lon']:.2f}.")
    evidence = {"jurisdiction": pack.name, "generated": now.isoformat(),
                "candidates": [{k: c[k] for k in ("id", "route", "score", "lat", "lon", "explanation")} for c in candidates[:8]],
                "risk_today": dict(class_hist), "area_high_or_above_km2": area_by_lead[0]}
    brief = {"deterministic": lines, "llm": llm.brief(evidence)}

    summary = {
        "schema": SCHEMA, "pipeline_version": __version__, "generated_at": now.isoformat(),
        "run": {"id": os.environ.get("GITHUB_RUN_ID", "local"), "commit": os.environ.get("GITHUB_SHA", "")[:12],
                "duration_s": round(time.monotonic() - t0, 1),
                "mode": "fixtures" if fixtures else ("replay" if as_of else "live"), "label": label},
        "jurisdiction": {"id": out_id, "pack": pack.id,
                         "name": f"{pack.name} · replay {now:%d %b %Y %H:%M} UTC{' · ' + label if label else ''}" if as_of else pack.name, "units": pack.units, "timezone": pack.timezone,
                         "terminology": pack.terminology},
        "kpis": {
            "candidates_by_route": dict(routes),
            "detections_48h": len(recent),
            "detections_48h_by_sensor": dict(Counter(o.platform for o in recent)),
            "known_incidents": len(incidents),
            "area_high_or_above_km2": area_by_lead,
            "peak_fwi_today": peak["days"][0]["fwi"] if peak else None,
            "peak_cell": peak["id"] if peak else None,
            "risk_class_histogram_today": dict(class_hist),
            "grid_cells": len(cells),
        },
        "brief": brief,
        "sources": [
            {**s.model_dump(mode="json"),
             "latency_min": round((s.fetched_at - s.latest_observation).total_seconds() / 60)
             if s.latest_observation and s.provider == "nasa_firms" else None}
            for s in sources],
        "model_card": {"id": pack.risk.model_id, "primary_index": pack.risk.primary_index,
                       "validation_status": pack.risk.validation.status, "validated_for": pack.id
                       if pack.risk.validation.status == "validated" else None,
                       "note": pack.risk.validation.note,
                       "method": "Canadian FWI System (Van Wagner 1987) from hourly NWP, spun up over "
                                 f"{pack.providers.weather[0].past_days if pack.providers.weather else 0} days; "
                                 "Fosberg FFWI and surface Hot-Dry-Windy Index as supplementary drivers."},
        "disclaimer": "Decision support only. Not an authoritative source for dispatch, evacuation or public warning.",
    }
    config = {**pack.public_summary(), "grid": {"spacing_deg": spacing, "cells": len(points)}}

    risk_fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": i, "geometry": {"type": "Polygon", "coordinates": _cell_polygon(c["lat"], c["lon"], spacing / 2)},
        "properties": {"id": c["id"], **{f"c{d['lead_days']}": d["class_index"] for d in c["days"]},
                       **{f"f{d['lead_days']}": d["fwi"] for d in c["days"]}}} for i, c in enumerate(cells)]}
    det_fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [o.lon, o.lat]},
        "properties": {"id": o.id, "sensor": o.sensor, "platform": o.platform, "dataset": o.dataset,
                       "time": o.observation_time.isoformat(), "ingested": o.ingestion_time.isoformat(),
                       "age_h": round((now - o.observation_time).total_seconds() / 3600, 1),
                       "confidence": o.confidence, "confidence_raw": o.confidence_raw, "frp": o.frp_mw,
                       "daynight": o.daynight, "res_m": o.native_resolution_m}} for o in observations]}
    inc_fc = {"type": "FeatureCollection", "features": []}
    for inc in incidents:
        props = inc.model_dump(mode="json", exclude={"perimeter"})
        inc_fc["features"].append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [inc.lon, inc.lat]},
                                   "properties": {**props, "role": "point"}})
        if inc.perimeter:
            inc_fc["features"].append({"type": "Feature", "geometry": inc.perimeter,
                                       "properties": {"id": inc.id, "name": inc.name, "role": "perimeter"}})

    if as_of:
        summary["sources"].append({"provider": "nifc_wfigs", "dataset": "current incidents", "status": "skipped",
                                   "message": "current-incident feed has no history; compare with the published perimeter",
                                   "fetched_at": now.isoformat(), "latest_observation": None, "records": 0,
                                   "native_resolution": "-", "expected_refresh": "-", "license": "-", "attribution": "-",
                                   "source_uri": "-", "latency_min": None})
    out = out_root / out_id
    out.mkdir(parents=True, exist_ok=True)
    files = {"summary.json": summary, "config.json": config, "risk.geojson": risk_fc,
             "risk_cells.json": {c["id"]: c for c in cells}, "detections.geojson": det_fc,
             "candidates.json": candidates, "incidents.geojson": inc_fc,
             "boundary.geojson": pack.boundary_geojson, "places.geojson": pack.places_geojson}
    for name, obj in files.items():
        _write(out / name, obj)
    manifest = {"schema": SCHEMA, "generated_at": now.isoformat(), "jurisdiction": out_id,
                "files": {n: hashlib.sha256((out / n).read_bytes()).hexdigest() for n in files}}
    _write(out / "manifest.json", manifest)
    log.info("%s: %d obs, %d candidates, %d cells, %d incidents in %.1fs", pack.id, len(observations),
             len(candidates), len(cells), len(incidents), time.monotonic() - t0)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--jurisdiction", "-j", action="append")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("site/data"))
    ap.add_argument("--cache", type=Path, default=Path(".cache"))
    ap.add_argument("--fixtures", action="store_true", help="offline run on synthetic inputs (labelled)")
    ap.add_argument("--strict", action="store_true", help="exit 1 if every live source failed")
    ap.add_argument("--default", help="jurisdiction the dashboard opens first")
    ap.add_argument("--as-of", type=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
                    help="replay a past moment, e.g. 2024-02-27T21:00Z (archived FIRMS + HRRR)")
    ap.add_argument("--label", default="", help="label for a replay, e.g. 'Smokehouse Creek Fire'")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ids = list_packs() if a.all else a.jurisdiction
    if a.default in ids:
        ids = [a.default] + [i for i in ids if i != a.default]
    a.out.mkdir(parents=True, exist_ok=True)
    if a.as_of and a.as_of.tzinfo is None:
        a.as_of = a.as_of.replace(tzinfo=timezone.utc)
    summaries = []
    for j in ids:
        summaries.append(run(j, a.out, fixtures=a.fixtures, cache_dir=a.cache, as_of=a.as_of, label=a.label))
    # merge with entries from earlier invocations (e.g. live run + replay run in one workflow)
    index = a.out / "jurisdictions.json"
    try:
        prior = json.loads(index.read_text()) if index.exists() else []
    except json.JSONDecodeError:
        prior = []
    new = [{"id": s["jurisdiction"]["id"], "name": s["jurisdiction"]["name"],
            "generated_at": s["generated_at"], "mode": s["run"]["mode"]} for s in summaries]
    ids_new = {e["id"] for e in new}
    merged = new + [e for e in prior if e["id"] not in ids_new and (a.out / e["id"] / "manifest.json").exists()]
    merged = [e for e in merged if "-replay-" not in e["id"]] + [e for e in merged if "-replay-" in e["id"]]
    _write(index, merged)
    if a.strict and all(src["status"] == "failed" for s in summaries for src in s["sources"]):
        log.error("every source failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
