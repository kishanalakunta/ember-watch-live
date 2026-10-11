"""Detection intelligence on top of established fire products (PRD §7.2).

We do not re-detect fire in pixels. We add:
  1. spatio-temporal clustering of observations           (FR-11)
  2. pass-aware multi-sensor confidence fusion             (FR-13, UC-D2)
  3. persistent static-source screening (gas flares etc.)
  4. cross-reference with authoritative incidents           (FR-12)
  5. fire-weather and exposure context                      (FR-16, FR-17)
  6. routing to a recommended human action                  (PRD A5: decision support only)
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime, timedelta

from ..geo import angle_diff, bearing_deg, compass, haversine_km, point_in_geometry, point_in_ring
from ..models import Incident, Observation

ROUTES = {
    "priority_review": "Likely new fire: seen on more than one satellite look and no known fire is there. Tell the duty officer, who decides.",
    "verify": "Needs a check: send a camera, drone or crew to confirm before anyone acts.",
    "watch": "Weak signal: keep an eye on it and look again after the next satellite pass.",
    "monitor_known": "Known fire: these heat spots are added to the fire the fire service already tracks.",
    "likely_static": "Likely a gas flare or industrial heat source, so it is kept out of the urgent list.",
}


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def cluster(obs: list[Observation], radius_km: float, window_h: float,
            spread_kmh: float = 0.0) -> list[list[Observation]]:
    """Single-linkage clustering. Two observations link when they are within
    `radius_km + spread_kmh * dt` (dt capped at 6 h), so a fast-moving
    wind-driven front observed on successive passes remains one fire."""
    if not obs:
        return []
    max_link = radius_km + spread_kmh * min(window_h, 6.0)
    cell = max_link / 111.0
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, o in enumerate(obs):
        grid[(int(math.floor(o.lat / cell)), int(math.floor(o.lon / cell)))].append(i)
    uf = _UF(len(obs))
    win = timedelta(hours=window_h)
    for (gy, gx), members in grid.items():
        # cells are max_link tall; longitude degrees shrink with latitude, so widen the x search
        span = max(1, math.ceil(1 / max(0.2, math.cos(math.radians(gy * cell)))))
        neigh = [j for dy in (-1, 0, 1) for dx in range(-span, span + 1) for j in grid.get((gy + dy, gx + dx), [])]
        for i in members:
            for j in neigh:
                if j <= i:
                    continue
                a, b = obs[i], obs[j]
                dt = abs(a.observation_time - b.observation_time)
                if dt > win:
                    continue
                allow = radius_km + spread_kmh * min(dt.total_seconds() / 3600, 6.0)
                if haversine_km(a.lat, a.lon, b.lat, b.lon) <= allow:
                    uf.union(i, j)
    groups: dict[int, list[Observation]] = defaultdict(list)
    for i, o in enumerate(obs):
        groups[uf.find(i)].append(o)
    return list(groups.values())


def _passes(members: list[Observation]) -> list[dict]:
    """Group into satellite passes; pixels within one pass are not independent evidence."""
    passes: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in members:
        bucket = o.observation_time.replace(minute=(o.observation_time.minute // 15) * 15, second=0)
        passes[(o.platform, bucket.isoformat())].append(o)
    out = []
    for (platform, _), ps in passes.items():
        pmax = max(p.confidence for p in ps)
        # extra adjacent pixels add a little, but with strong diminishing returns
        p = 1 - (1 - pmax) * (0.85 ** (len(ps) - 1))
        out.append({"platform": platform, "sensor": ps[0].sensor, "time": min(x.observation_time for x in ps),
                    "pixels": len(ps), "p": min(0.98, p), "frp_sum": sum(x.frp_mw or 0 for x in ps),
                    "obs_ids": [x.id for x in ps]})
    return sorted(out, key=lambda x: x["time"])


def _windows(passes: list[dict], gap_h: float) -> int:
    """Count overpass windows. NOAA-20, NOAA-21 and S-NPP fly the same orbit about
    50 minutes apart, so three looks within two hours are one window, not three
    independent confirmations of a fire that is still burning later."""
    n, last = 0, None
    for p in passes:   # sorted by time
        if last is None or (p["time"] - last).total_seconds() / 3600 > gap_h:
            n += 1
        last = p["time"]
    return n


def _nearest_cell(lat, lon, cells):
    best, bd = None, 1e9
    for c in cells:
        d = haversine_km(lat, lon, c["lat"], c["lon"])
        if d < bd:
            best, bd = c, d
    return best, bd


def _match_incident(lat, lon, incidents: list[Incident], radius_km: float):
    best = None
    for inc in incidents:
        inside = bool(inc.perimeter and point_in_geometry(lon, lat, inc.perimeter))
        d = haversine_km(lat, lon, inc.lat, inc.lon)
        if inside or d <= radius_km:
            score = -1 if inside else d
            if best is None or score < best[0]:
                best = (score, inc, d, inside)
    if not best:
        return None
    _, inc, d, inside = best
    return {"id": inc.id, "name": inc.name, "distance_km": round(d, 1), "inside_perimeter": inside,
            "size_acres": inc.size_acres, "percent_contained": inc.percent_contained}


def _exposure(lat, lon, places_fc, radius_km, wind_dir_from):
    out = []
    wind_to = None if wind_dir_from is None else (wind_dir_from + 180) % 360
    for f in places_fc.get("features", []):
        plon, plat = f["geometry"]["coordinates"]
        d = haversine_km(lat, lon, plat, plon)
        if d > radius_km:
            continue
        b = bearing_deg(lat, lon, plat, plon)
        out.append({"name": f["properties"]["name"], "population": f["properties"].get("population"),
                    "distance_km": round(d, 1), "bearing": round(b), "direction": compass(b),
                    "downwind": wind_to is not None and angle_diff(b, wind_to) <= 45})
    return sorted(out, key=lambda x: (not x["downwind"], x["distance_km"]))[:6]


def build_candidates(obs, incidents, cells, pack, now: datetime) -> list[dict]:
    det = pack.detection
    inside = [o for o in obs if point_in_ring(o.lon, o.lat, pack.boundary_ring)]
    clusters = cluster(inside, det.cluster_radius_km, det.cluster_window_hours, det.max_spread_kmh)
    recent_cut = now - timedelta(hours=det.cluster_window_hours)
    out = []
    for members in clusters:
        last_seen = max(o.observation_time for o in members)
        if last_seen < recent_cut:
            continue
        members.sort(key=lambda o: o.observation_time)
        passes = _passes(members)
        evidence = 1.0
        for p in passes:
            evidence *= (1 - p["p"])
        evidence = 1 - evidence
        lat = sum(o.lat for o in members) / len(members)
        lon = sum(o.lon for o in members) / len(members)
        extent = 2 * max(haversine_km(lat, lon, o.lat, o.lon) for o in members)
        days = {o.observation_time.date() for o in members}
        max_px = max(p["pixels"] for p in passes)
        static = (len(days) >= det.static_source.min_distinct_days and extent <= det.static_source.max_extent_km
                  and max_px <= 2)
        cell, cell_km = _nearest_cell(lat, lon, cells)
        d0 = cell["days"][0] if cell else None
        cur = cell["current"] if cell else {}
        wfac, wnote = 1.0, "no weather context"
        if d0:
            wfac = {0: 0.85, 1: 0.92}.get(d0["class_index"], 1.0)
            if d0["rain_24h_mm"] >= 5:
                wfac = min(wfac, 0.85)
            wnote = f"{d0['class']} fire weather (FWI {d0['fwi']})"
        score = evidence * wfac * (0.25 if static else 1.0)
        incident = _match_incident(lat, lon, incidents, det.incident_match_km)
        frp_max = max((o.frp_mw or 0) for o in members)
        # Priority review needs corroboration: still burning on a later overpass window, or one
        # look that is unmistakable (3+ adjacent pixels with a strong FRP). A short-lived bright
        # spot is usually a farm burn, a flare or glint, so it goes to verification instead.
        windows = _windows(passes, det.routing.window_gap_hours)
        corroborated = windows >= det.routing.min_passes_for_priority or (max_px >= 3 and frp_max >= 10)
        stale = (now - last_seen).total_seconds() / 3600 > det.routing.stale_after_hours
        held_back = False
        if incident:
            route = "monitor_known"
        elif static:
            route = "likely_static"
        elif stale:
            route = "watch"
        elif score >= det.routing.priority_review and corroborated:
            route = "priority_review"
        elif score >= det.routing.verify:
            route = "verify"
            held_back = score >= det.routing.priority_review
        else:
            route = "watch"
        first = members[0]
        cid = "C-" + hashlib.sha256(f"{first.id}".encode()).hexdigest()[:6].upper()
        pixel_m = max(o.native_resolution_m for o in members)
        sensors = sorted({p["platform"] for p in passes})
        latest_pass_px = passes[-1]["pixels"]
        first_pass_px = passes[0]["pixels"]
        cand = {
            "id": cid, "lat": round(lat, 5), "lon": round(lon, 5),
            "route": route, "recommendation": ROUTES[route],
            "score": round(score, 3),
            "components": {
                "detection_evidence": round(evidence, 3), "weather_factor": wfac,
                "static_factor": 0.25 if static else 1.0,
                "passes": len(passes), "platforms": sensors, "independent_platforms": len(sensors),
                "observations": len(members), "distinct_days": len(days),
                "extent_km": round(extent, 2), "max_pixels_per_pass": max_px,
                "overpass_windows": windows, "stale": stale,
                "corroborated": corroborated, "held_back_uncorroborated": held_back,
                "growth": "growing" if latest_pass_px > first_pass_px else ("shrinking" if latest_pass_px < first_pass_px else "steady"),
            },
            "first_seen": members[0].observation_time.isoformat(), "last_seen": last_seen.isoformat(),
            "hours_since_last": round((now - last_seen).total_seconds() / 3600, 1),
            "frp_total_mw": round(sum(o.frp_mw or 0 for o in members), 1),
            "frp_max_mw": round(max((o.frp_mw or 0) for o in members), 1),
            "location_uncertainty_km": round(max(extent / 2, pixel_m / 1000 * 0.7), 2),
            "passes": [{**p, "time": p["time"].isoformat(), "p": round(p["p"], 3), "frp_sum": round(p["frp_sum"], 1)} for p in passes],
            "incident": incident,
            "weather": None if not d0 else {
                "cell_id": cell["id"], "cell_distance_km": round(cell_km, 1), "class": d0["class"], "fwi": d0["fwi"],
                "isi": d0["isi"], "ffmc": d0["ffmc"], "rh_min": d0["rh_min"], "dry_days": d0["dry_days"],
                "now": cur, "note": wnote},
            "exposure": _exposure(lat, lon, pack.places_geojson, det.exposure_radius_km, cur.get("wind_dir")),
            "evidence_ids": [o.id for o in members],
        }
        if route == "verify":
            cand["verification_task"] = {
                "search_radius_km": round(max(0.5, cand["location_uncertainty_km"] * 1.5), 2),
                "sensors": ["thermal (LWIR)", "RGB"],
                "aviation_profile": pack.aviation.regulation_profile,
                "note": "Recommendation only. Flight approval and airspace checks remain with the aviation lead.",
            }
        out.append(cand)
    order = ["priority_review", "verify", "monitor_known", "watch", "likely_static"]
    out.sort(key=lambda c: (order.index(c["route"]), -c["score"]))
    return out
