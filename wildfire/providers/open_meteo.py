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
TIME_BUDGET_S = 420      # stop asking after 7 minutes so one slow source cannot stall the whole run
CACHE_TTL_H = 6
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
    key = hashlib.sha256(json.dumps([model, ref.fallback_model, points, ref.past_days, ref.forecast_days,
                                     now.strftime("%Y-%m-%d"), bool(as_of)]).encode()).hexdigest()[:16]
    cache = cache_dir / f"weather-{pack.id}-{key}.json" if cache_dir else None
    if cache and cache.is_file() and (time.time() - cache.stat().st_mtime) < CACHE_TTL_H * 3600:
        items = json.loads(cache.read_text())
        status, msg = "ok", f"cached pull from {datetime.fromtimestamp(cache.stat().st_mtime, timezone.utc):%H:%M} UTC"
    else:
        items, filled, failed, first_error = [], 0, 0, ""
        size = HIST_BATCH if as_of else BATCH
        started = time.monotonic()
        for i in range(0, len(points), size):
            batch = points[i:i + size]
            if time.monotonic() - started > TIME_BUDGET_S:
                first_error = first_error or f"stopped after {TIME_BUDGET_S} s time budget"
                failed += len(batch)
                items.extend({"latitude": p[0], "longitude": p[1], "hourly": None} for p in batch)
                continue
            try:
                got = _request(batch, model, ref.past_days, ref.forecast_days, as_of)
            except http.FetchError as e:
                log.warning("model %s failed (%s); using %s", model, e, ref.fallback_model)
                first_error = first_error or f"{model}: {e}"
                got = []
            if ref.fallback_model and (not got or any(_null_frac(g["hourly"]) > 0.02 for g in got)):
                try:
                    fb = _request(batch, ref.fallback_model, ref.past_days, ref.forecast_days, as_of)
                    if not got:
                        got = fb
                    else:
                        for g, f in zip(got, fb, strict=False):
                            if _null_frac(g["hourly"]) > 0:
                                g["hourly"] = _merge(g["hourly"], f["hourly"])
                                filled += 1
                except http.FetchError as e:
                    log.error("fallback model failed too: %s", e)
                    first_error = first_error or f"{ref.fallback_model}: {e}"
            if not got:
                failed += len(batch)
                got = [{"latitude": p[0], "longitude": p[1], "hourly": None} for p in batch]
            items.extend(got)
            time.sleep(0.4)  # be polite to a free service
        if cache and not failed:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(items))
        status = "ok" if not failed else ("partial" if failed < len(points) else "failed")
        msg = f"{filled} cells gap-filled from {ref.fallback_model}; {failed} cells missing" if (filled or failed) else ""
        if first_error:
            msg += f" | first error: {first_error[:220]}"
    series = [to_series(it) if it.get("hourly") else None for it in items]
    return series, [SourceMeta(status=status, records=sum(s is not None for s in series),
                               latest_observation=now, message=msg, **meta)]


def is_finite(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))
