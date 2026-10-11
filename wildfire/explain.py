"""Evidence-linked explanations (FR-14, FR-28, UC-D3, PRD Screen 7).

Statements are generated deterministically from structured results and are
tagged Observed / Calculated / Model-derived / Unknown, each with the IDs
of the evidence it rests on. The optional LLM layer (llm.py) may rephrase
these, but every number it outputs is checked against this evidence.
"""
from __future__ import annotations

from datetime import datetime


class Units:
    def __init__(self, system: str):
        self.imperial = system == "imperial"

    def dist(self, km: float) -> str:
        return f"{km * 0.621371:.1f} mi" if self.imperial else f"{km:.1f} km"

    def speed(self, kmh: float) -> str:
        return f"{kmh / 1.609344:.0f} mph" if self.imperial else f"{kmh:.0f} km/h"

    def temp(self, c: float) -> str:
        return f"{c * 9 / 5 + 32:.0f} °F" if self.imperial else f"{c:.0f} °C"


def _t(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d %b %H:%M UTC")


def explain_candidate(c: dict, units: Units, sources: dict[str, str]) -> list[dict]:
    comp, out = c["components"], []

    def add(kind, text, refs):
        out.append({"kind": kind, "text": text, "evidence": refs})

    pass_txt = "; ".join(f"{p['sensor']} {p['platform']} at {_t(p['time'])} ({p['pixels']} px)" for p in c["passes"][:4])
    add("observed", f"{comp['observations']} thermal observations from {comp['passes']} satellite pass"
        f"{'es' if comp['passes'] != 1 else ''} on {comp['independent_platforms']} platform"
        f"{'s' if comp['independent_platforms'] != 1 else ''}: {pass_txt}.", c["evidence_ids"][:12])
    add("observed", f"Fire radiative power totals {c['frp_total_mw']} MW (max pixel {c['frp_max_mw']} MW); "
        f"the cluster spans {units.dist(comp['extent_km'])} and is {comp['growth']} between first and latest pass.",
        c["evidence_ids"][:12])
    if c["incident"]:
        i = c["incident"]
        where = "inside the mapped perimeter of" if i["inside_perimeter"] else f"{units.dist(i['distance_km'])} from"
        add("observed", f"Located {where} known incident {i['name']} ({i['id']}).", [sources.get("incidents", "incidents")])
    else:
        add("calculated", "No authoritative incident within the configured matching radius.",
            [sources.get("incidents", "incidents (no feed configured)")])
    add("calculated", f"Fused detection confidence {comp['detection_evidence']:.2f} (pass-aware noisy-OR), "
        f"weather factor {comp['weather_factor']}, static-source factor {comp['static_factor']}; "
        f"priority score {c['score']:.2f}.", [c["id"]])
    if comp.get("held_back_uncorroborated"):
        add("calculated", "Score is high, but it has only been seen in one overpass window (satellites passing "
            "within a few hours count once), so it is held at verification until a later pass or a "
            "camera/UAS check confirms it.", [c["id"]])
    if comp.get("stale"):
        add("calculated", f"Not seen for {c['hours_since_last']} h, so the latest satellite pass missed it; "
            "it is kept as a weak signal rather than a new fire.", [c["id"]])
    if comp["static_factor"] < 1:
        add("model", f"Detected on {comp['distinct_days']} separate days within {units.dist(comp['extent_km'])} "
            "with at most 2 pixels per pass, which matches a static heat source such as a gas flare.", [c["id"]])
    w = c.get("weather")
    if w:
        now = w.get("now") or {}
        bits = []
        if now.get("wind_kmh") is not None and now.get("wind_dir") is not None:
            from .geo import compass
            bits.append(f"wind from the {compass(now['wind_dir'])} at {units.speed(now['wind_kmh'])}")
        if now.get("rh") is not None:
            bits.append(f"RH {now['rh']:.0f}%")
        add("model", f"Fire weather at the nearest grid cell: {w['class']} (FWI {w['fwi']}, ISI {w['isi']}, "
            f"FFMC {w['ffmc']}); {w['dry_days']} days without wetting rain"
            + (f"; current {', '.join(bits)}." if bits else "."), [w["cell_id"], sources.get("weather", "weather")])
    dw = [e for e in c["exposure"] if e["downwind"]]
    if dw:
        e = dw[0]
        add("calculated", f"{len(dw)} populated place{'s' if len(dw) > 1 else ''} downwind within the exposure radius; "
            f"nearest is {e['name']} at {units.dist(e['distance_km'])} {e['direction']}.", ["places"])
    elif c["exposure"]:
        e = c["exposure"][0]
        add("calculated", f"Nearest populated place: {e['name']}, {units.dist(e['distance_km'])} {e['direction']} (not downwind).", ["places"])
    unknowns = ["Ground truth: no field, camera or UAS confirmation is recorded yet."]
    if c["location_uncertainty_km"] >= 0.5:
        unknowns.append(f"Exact ignition point: satellite pixels limit location to about ±{units.dist(c['location_uncertainty_km'])}.")
    if c["hours_since_last"] > 6:
        unknowns.append(f"Current activity: last satellite detection was {c['hours_since_last']} h ago.")
    for u in unknowns:
        add("unknown", u, [])
    return out
