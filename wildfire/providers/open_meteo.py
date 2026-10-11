"""Open-Meteo weather adapter (FR-6 inputs).

One adapter serves several numerical weather models (HRRR, ECMWF IFS, ...);
the jurisdiction pack picks the model. Hourly series include `past_days` of
analysis so the Fire Weather Index moisture codes can be spun up.

Rate-limit note: the free tier allows 10k weighted calls/day for
non-commercial use. Responses are cached on disk for CACHE_TTL_H hours so the
30-minute detection runs reuse the last weather pull.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import http
from ..models import SourceMeta

log = logging.getLogger("wildfire.weather")
URL = "https://api.open-meteo.com/v1/forecast"
HIST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"   # archived model runs, for replays
http.ALLOWED_HOSTS.add("historical-forecast-api.open-meteo.com")
VARS = ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m",
        "wind_gusts_10m", "precipitation", "vapour_pressure_deficit"]
LICENSE = "CC BY 4.0 (Open-Meteo); underlying model data per NOAA/ECMWF terms. Free tier: non-commercial."
ATTRIB = "Weather data by Open-Meteo.com"
BATCH = 50
HIST_BATCH = 15          # the archive API is slower and its call weight grows with the date range
TIME_BUDGET_S = 240      # per pull: stop asking after 4 minutes so one slow source cannot stall the run
CACHE_TTL_H = 3          # recent days + forecast
HIST_TTL_H = 24          # 60-day spin-up history
STALE_MAX_H = 72         # oldest weather we will show when a pull fails
RECENT_DAYS = 3
RES = {"gfs_hrrr": "3 km (HRRR)", "ecmwf_ifs025": "0.25° (~25 km, ECMWF IFS)", "best_match": "model blend"}


def _request(points, model, past_days, forecast_days, as_of=None) -> list[dict]:
    params = {
        "latitude": ",".join(f"{p[0]:.4f}" for p in points),
        "longitude": ",".join(f"{p[1]:.4f}" for p in points),
        "hourly": ",".join(VARS), "wind_speed_unit": "kmh", "timezone": "GMT", "models": model,
    }
    if as_of:
        params["start_date"] = f"{as_of - timedelta(days=past_days):%Y-%m-%d}"
        params["end_date"] = f"{as_of + timedelta(days=forecast_days - 1):%Y-%m-%d}"
    else:
        params.update(past_days=past_days, forecast_days=forecast_days)
    data = http.get(HIST_URL if as_of else URL, params=params, timeout=120).json()
    return data if isinstance(data, list) else [data]


def _null_frac(series: dict) -> float:
    vals = [v for k in ("temperature_2m", "relative_humidity_2m", "wind_speed_10m") for v in series.get(k, [])]
    return sum(v is None for v in vals) / max(1, len(vals))


def _merge(primary: dict, fallback: dict) -> dict:
    """Fill gaps in the primary model's hourly series with the fallback model."""
    out = {"time": primary["time"]}
    fb_index = {t: i for i, t in enumerate(fallback.get("time", []))}
    for k in VARS:
        p = primary.get(k, [None] * len(primary["time"]))
        f = fallback.get(k, [])
        out[k] = [pv if pv is not None else (f[fb_index[t]] if t in fb_index and fb_index[t] < len(f) else None)
                  for t, pv in zip(primary["time"], p, strict=False)]
    return out


def to_series(item: dict) -> dict:
    h = item["hourly"]
    times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h["time"]]
    return {"time": times, **{k: h.get(k, [None] * len(times)) for k in VARS}}


def _pull(points, model, fallback, past_days, forecast_days, as_of, batch_size):
    """Fetch every point in batches, filling gaps from the fallback model.
    Returns (items, filled, failed, first_error)."""
    items, filled, failed, first_error = [], 0, 0, ""
    started = time.monotonic()
    for i in range(0, len(points), batch_size):
        batch = points[i:i + batch_size]
        if time.monotonic() - started > TIME_BUDGET_S:
            first_error = first_error or f"stopped after {TIME_BUDGET_S} s time budget"
            failed += len(batch)
            items.extend({"latitude": p[0], "longitude": p[1], "hourly": None} for p in batch)
            continue
        try:
            got = _request(batch, model, past_days, forecast_days, as_of)
        except http.FetchError as e:
            log.warning("model %s failed (%s); using %s", model, e, fallback)
            first_error = first_error or f"{model}: {e}"
            got = []
        if fallback and (not got or any(_null_frac(g["hourly"]) > 0.02 for g in got)) \
                and not http.is_quota_error(first_error):
            try:
                fb = _request(batch, fallback, past_days, forecast_days, as_of)
                if not got:
                    got = fb
                else:
                    for g, f in zip(got, fb, strict=False):
                        if _null_frac(g["hourly"]) > 0:
                            g["hourly"] = _merge(g["hourly"], f["hourly"])
                            filled += 1
            except http.FetchError as e:
                log.error("fallback model failed too: %s", e)
                first_error = first_error or f"{fallback}: {e}"
        if not got:
            failed += len(batch)
            got = [{"latitude": p[0], "longitude": p[1], "hourly": None} for p in batch]
        items.extend(got)
        if http.is_quota_error(first_error):
            # hourly/daily quota: further calls will fail the same way, so stop now
            rest = points[i + batch_size:]
            failed += len(rest)
            items.extend({"latitude": p[0], "longitude": p[1], "hourly": None} for p in rest)
            break
        time.sleep(0.4)  # be polite to a free service
    return items, filled, failed, first_error


def _join(hist: dict | None, recent: dict | None) -> dict | None:
    """Join a long history series with a fresher short one; the fresh values win."""
    if not hist or not recent:
        return recent or hist
    by_t = {t: i for i, t in enumerate(recent["time"])}
    times = sorted(set(hist["time"]) | set(recent["time"]))
    hidx = {t: i for i, t in enumerate(hist["time"])}
    out = {"time": times}
    for k in VARS:
        h, r = hist.get(k) or [], recent.get(k) or []
        col = []
        for t in times:
            v = r[by_t[t]] if t in by_t and by_t[t] < len(r) else None
            if v is None and t in hidx and hidx[t] < len(h):
                v = h[hidx[t]]
            col.append(v)
        out[k] = col
    return out


def _cached(cache_dir, name, ttl_h, pull):
    """Fresh cache -> use it. Otherwise pull; if the pull fails, fall back to the last
    good copy (up to STALE_MAX_H old) rather than leaving the map without weather."""
    path = cache_dir / name if cache_dir else None
    age_h = (time.time() - path.stat().st_mtime) / 3600 if path and path.is_file() else None
    if age_h is not None and age_h < ttl_h:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return json.loads(path.read_text()), 0, 0, "", f"cached pull from {stamp:%H:%M} UTC"
    items, filled, failed, err = pull()
    if not failed:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items))
        return items, filled, 0, err, ""
    if age_h is not None and age_h < STALE_MAX_H:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return json.loads(path.read_text()), 0, 0, err, f"latest pull failed, using weather from {stamp:%d %b %H:%M} UTC"
    return items, filled, failed, err, ""


def fetch(pack, ref, points, *, now: datetime, cache_dir: Path | None, fixtures: Path | None = None,
          as_of: datetime | None = None):
    model = ref.model or "best_match"
    meta = dict(provider="open_meteo", dataset=f"hourly forecast + {ref.past_days} d analysis ({model})",
                fetched_at=now, native_resolution=RES.get(model, model),
                **({"source_uri": HIST_URL} if as_of else {}),
                expected_refresh="Hourly (HRRR) / 6-hourly (global models)",
                license=LICENSE, attribution=ATTRIB)
    meta.setdefault("source_uri", URL)
    if fixtures:
        from ..fixtures import synthetic_weather
        items = synthetic_weather(points, now, ref.past_days, ref.forecast_days)
        series = [to_series(it) for it in items]
        return series, [SourceMeta(status="fixture", records=len(series), latest_observation=now,
                                   message="Synthetic weather in Open-Meteo response format", **meta)]

    def key(*parts):
        return hashlib.sha256(json.dumps([model, ref.fallback_model, points, *parts]).encode()).hexdigest()[:16]

    notes = []
    if as_of:
        items, filled, failed, err, note = _cached(
            cache_dir, f"weather-{pack.id}-asof-{key(ref.past_days, ref.forecast_days, as_of.isoformat())}.json",
            24 * 365, lambda: _pull(points, model, ref.fallback_model, ref.past_days, ref.forecast_days,
                                    as_of, HIST_BATCH))
        notes.append(note)
    else:
        # The 60-day spin-up history changes slowly: pull it once a day. The last few days
        # plus the forecast are pulled every few hours. This keeps us well inside the free quota.
        h_items, h_filled, h_failed, h_err, h_note = _cached(
            cache_dir, f"weather-{pack.id}-hist-{key(ref.past_days)}.json", HIST_TTL_H,
            lambda: _pull(points, model, ref.fallback_model, ref.past_days, 1, None, BATCH))
        r_items, r_filled, r_failed, r_err, r_note = _cached(
            cache_dir, f"weather-{pack.id}-recent-{key(RECENT_DAYS, ref.forecast_days)}.json", CACHE_TTL_H,
            lambda: _pull(points, model, ref.fallback_model, RECENT_DAYS, ref.forecast_days, None, BATCH))
        items = []
        for p, h, r in zip(points, h_items, r_items, strict=False):
            joined = _join(h.get("hourly"), r.get("hourly"))
            items.append({"latitude": p[0], "longitude": p[1], "hourly": joined})
        filled, err = h_filled + r_filled, r_err or h_err
        failed = sum(1 for it in items if not it.get("hourly"))
        notes += [f"history: {h_note}" if h_note else "", f"recent: {r_note}" if r_note else ""]
        if h_failed and not r_failed:
            notes.append(f"history missing for {h_failed} cells, so fire-weather codes start from defaults")
    status = "ok" if not failed else ("partial" if failed < len(points) else "failed")
    if any("latest pull failed" in n for n in notes) and status == "ok":
        status = "partial"
    msg = "; ".join(n for n in notes if n)
    if filled or failed:
        msg = "; ".join(x for x in (msg, f"{filled} cells gap-filled from {ref.fallback_model}; {failed} cells missing") if x)
    if err and status != "ok":
        msg += f" | first error: {err[:220]}"
    series = [to_series(it) if it.get("hourly") else None for it in items]
    return series, [SourceMeta(status=status, records=sum(s is not None for s in series),
                               latest_observation=now, message=msg, **meta)]


def is_finite(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))
