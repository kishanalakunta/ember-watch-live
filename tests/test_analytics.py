from datetime import datetime, timedelta, timezone

import pytest

from wildfire.analytics import fwi
from wildfire.analytics.detect import build_candidates, cluster
from wildfire.analytics.indices import ffwi, hdw
from wildfire.config import load_pack, list_packs
from wildfire.geo import haversine_km
from wildfire.models import Observation
from wildfire.providers.firms import parse_csv
from wildfire import llm

NOW = datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)


# --- FWI: Van Wagner & Pickett (1985) standard test sequence ------------------
@pytest.mark.parametrize("wx,expect", [
    ((17, 42, 25, 0.0), (87.7, 8.5, 19.0, 10.9, 8.5, 10.1)),
    ((20, 21, 25, 2.4), (86.2, 10.4, 23.6, 8.8, 10.4, 9.3)),
])
def test_fwi_reference_values(wx, expect):
    state = (85.0, 6.0, 15.0)
    r = None
    seq = [(17, 42, 25, 0.0), (20, 21, 25, 2.4)]
    for day in seq[: seq.index(wx) + 1]:
        r = fwi.step(*day, 4, state)
        state = (r.ffmc, r.dmc, r.dc)
    got = tuple(round(x, 1) for x in (r.ffmc, r.dmc, r.dc, r.isi, r.bui, r.fwi))
    assert got == expect


def test_fwi_monotone_in_dryness():
    wet = fwi.step(25, 80, 10, 0, 7, (85, 6, 15))
    dry = fwi.step(25, 15, 10, 0, 7, (85, 6, 15))
    assert dry.ffmc > wet.ffmc and dry.fwi > wet.fwi


def test_ffwi_and_hdw_bounds():
    assert 90 <= ffwi(35, 5, 60) <= 100
    assert ffwi(10, 95, 5) < 10
    assert hdw(3.0, 36) == pytest.approx(300.0)


# --- FIRMS parsing -------------------------------------------------------------
CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight
35.80000,-100.40000,350.1,0.4,0.4,2026-07-01,1842,N20,VIIRS,h,2.0NRT,295.0,40.2,D
35.80300,-100.40200,340.1,0.4,0.4,2026-07-01,912,N,VIIRS,l,2.0NRT,290.0,,N
bad,row,,,,,,,,,,,,
"""


def test_parse_firms_csv():
    obs = parse_csv(CSV, "VIIRS_NOAA20_NRT", "texas", NOW)
    assert len(obs) == 2
    assert obs[0].platform == "NOAA-20" and obs[0].confidence == 0.9
    assert obs[1].observation_time.hour == 9 and obs[1].observation_time.minute == 12
    assert obs[1].frp_mw is None and obs[1].confidence == 0.3


def test_parse_firms_rejects_error_text():
    from wildfire.http import FetchError
    with pytest.raises(FetchError):
        parse_csv("Invalid MAP_KEY.", "VIIRS_SNPP_NRT", "texas", NOW)


# --- Clustering and routing ---------------------------------------------------
def _obs(lat, lon, t, conf=0.7, platform="NOAA-20", frp=5.0):
    return Observation(id=Observation.make_id(lat, lon, t), provider="t", dataset="d", sensor="VIIRS",
                       platform=platform, observation_time=t, ingestion_time=NOW, lat=lat, lon=lon,
                       native_resolution_m=375, confidence=conf, confidence_raw="n", frp_mw=frp,
                       license="x", attribution="x", jurisdiction="texas")


def test_cluster_links_moving_front_but_not_distant_fires():
    t = NOW - timedelta(hours=3)
    a = _obs(35.80, -100.40, t)
    b = _obs(35.83, -100.37, t + timedelta(hours=2))      # ~4.3 km later, 2 h on
    c = _obs(34.00, -101.00, t)                           # far away
    assert haversine_km(a.lat, a.lon, b.lat, b.lon) > 2
    groups = cluster([a, b, c], radius_km=2, window_h=36, spread_kmh=3)
    assert sorted(len(g) for g in groups) == [1, 2]
    assert len(cluster([a, b], radius_km=2, window_h=36, spread_kmh=0)) == 2


def _cells(class_index=3):
    return [{"id": "r1", "lat": 35.8, "lon": -100.4, "current": {"wind_dir": 225, "wind_kmh": 40, "rh": 12},
             "days": [{"class_index": class_index, "class": "Very high", "fwi": 30, "isi": 20, "ffmc": 94,
                       "rh_min": 10, "dry_days": 20, "rain_24h_mm": 0}]}]


def test_routing_multisensor_vs_single_pixel():
    pack = load_pack("texas")
    t = NOW - timedelta(hours=2)
    strong = [_obs(35.80, -100.40, t, 0.9, "NOAA-20"), _obs(35.801, -100.401, t + timedelta(hours=1), 0.7, "Suomi NPP")]
    weak = [_obs(33.40, -99.80, t, 0.7)]
    out = {c["components"]["observations"]: c for c in build_candidates(strong + weak, [], _cells(), pack, NOW)}
    assert out[2]["route"] == "priority_review"
    assert out[1]["route"] == "verify" and "verification_task" in out[1]


def test_static_flare_is_suppressed():
    pack = load_pack("texas")
    flare = [_obs(31.9012, -102.3307, NOW - timedelta(days=d, hours=1), 0.7, "Suomi NPP", 3) for d in range(3)]
    c = build_candidates(flare, [], _cells(), pack, NOW)
    assert len(c) == 1 and c[0]["route"] == "likely_static"


def test_downwind_exposure():
    pack = load_pack("texas")
    t = NOW - timedelta(hours=1)
    # fire SW of Amarillo-ish; wind from SW blows toward NE
    c = build_candidates([_obs(35.0, -102.0, t, 0.9)], [], _cells(), pack, NOW)[0]
    assert any(e["downwind"] for e in c["exposure"]) or c["exposure"] == []


# --- Config packs ---------------------------------------------------------------
def test_every_pack_loads_and_is_neutral():
    ids = list_packs()
    assert {"texas", "portugal"} <= set(ids)
    for i in ids:
        p = load_pack(i)
        assert p.boundary_ring[0] == p.boundary_ring[-1]            # closed ring
        assert p.places_geojson["features"]                          # exposure layer present
        assert p.risk.validation.status != "validated" or p.risk.validation.note


@pytest.mark.parametrize("bad", ["../etc", "TEXAS", "a/b", "", "x" * 60])
def test_pack_id_rejects_traversal(bad):
    with pytest.raises((ValueError, FileNotFoundError)):
        load_pack(bad)


# --- LLM guardrail -------------------------------------------------------------
def test_llm_verifier_drops_unsupported_numbers():
    evidence = '{"fwi": 30.2, "obs": 23}'
    sections = {"observed": [{"text": "23 observations recorded", "evidence": ["C-1"]},
                             {"text": "Fire is 4000 acres", "evidence": ["C-1"]}],
                "model_derived": [{"text": "FWI 30.2", "evidence": ["r1"]}]}
    clean, removed = llm.verify(sections, evidence)
    assert removed == 1
    assert [s["text"] for s in clean["observed"]] == ["23 observations recorded"]


def test_geonames_parse_clips_to_jurisdiction():
    from wildfire.providers.geonames import parse
    pack = load_pack("texas")
    rows = [
        "5528450\tPampa\tPampa\t\t35.53616\t-100.95987\tP\tPPLA2\tUS\t\tTX\t179\t\t\t16867\t\t983\tAmerica/Chicago\t2017-03-09",
        "5516233\tAmarillo\tAmarillo\t\t35.222\t-101.8313\tP\tPPLA2\tUS\t\tTX\t375\t\t\t199371\t\t1099\tAmerica/Chicago\t2017-03-09",
        "4544349\tOklahoma City\tOklahoma City\t\t35.46756\t-97.51643\tP\tPPLA\tUS\t\tOK\t109\t\t\t631346\t\t366\tAmerica/Chicago\t2019-09-05",
        "1\tTinyville\tTinyville\t\t33.0\t-99.0\tP\tPPL\tUS\t\tTX\t\t\t\t200\t\t\t\t",
        "2\tA Hill\tA Hill\t\t33.0\t-99.0\tT\tHLL\tUS\t\tTX\t\t\t\t5000\t\t\t\t",
    ]
    fc = parse("\n".join(rows), pack.boundary_ring)
    assert [f["properties"]["name"] for f in fc["features"]] == ["Amarillo", "Pampa"]


def test_replay_requests_archived_products(monkeypatch):
    """Replay mode must ask FIRMS for science-quality archives and Open-Meteo for archived runs."""
    from wildfire import http as H
    from wildfire.providers import firms as FP, open_meteo as OM
    calls = []

    class R:
        def __init__(self, text="", js=None):
            self.text, self._js = text, js
        def json(self):
            return self._js

    def fake_get(url, params=None, **kw):
        calls.append((url, params))
        if "firms" in url:
            return R(CSV)
        return R(js=[{"latitude": 35.0, "longitude": -100.0, "hourly": {"time": ["2024-02-27T00:00"], **{v: [1.0] for v in OM.VARS}}}])

    monkeypatch.setattr(H, "get", fake_get)
    monkeypatch.setenv("FIRMS_MAP_KEY", "testkey-123456")
    pack = load_pack("texas")
    as_of = datetime(2024, 2, 27, 21, tzinfo=timezone.utc)
    ref = pack.providers.active_fire[0]
    obs, metas = FP.fetch(pack, ref, now=as_of, as_of=as_of)
    assert any("/VIIRS_NOAA20_SP/" in u and u.endswith("/3/2024-02-25") for u, _ in calls)
    assert all("testkey" not in m.source_uri for m in metas)          # key never in provenance
    wref = pack.providers.weather[0]
    OM.fetch(pack, wref, [(35.0, -100.0)], now=as_of, cache_dir=None, as_of=as_of)
    u, p = calls[-1]
    assert u == OM.HIST_URL and p["start_date"] == "2023-12-29" and p["end_date"] == "2024-02-29"


def test_http_get_handles_gzip_bodies(monkeypatch):
    """Regression: NIFC/ArcGIS returns gzip; the body must be decoded exactly once."""
    import gzip
    import httpx
    from wildfire import http as H
    payload = b'{"type":"FeatureCollection","features":[]}'

    def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
                              content=gzip.compress(payload))
    monkeypatch.setattr(H, "_TRANSPORT", httpx.MockTransport(handler))
    r = H.get("https://services3.arcgis.com/x/query")
    assert r.json() == {"type": "FeatureCollection", "features": []}


def test_pipeline_survives_a_crashing_provider(monkeypatch, tmp_path):
    from wildfire import pipeline
    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setitem(pipeline.INCIDENTS, "nifc_wfigs", boom)
    s = pipeline.run("texas", tmp_path, fixtures=True, cache_dir=None)
    failed = [x for x in s["sources"] if x["status"] == "failed"]
    assert failed and "provider exploded" in failed[0]["message"]
    assert s["kpis"]["grid_cells"] > 0


def test_single_pass_needs_corroboration_for_priority():
    pack = load_pack("texas")
    t = NOW - timedelta(hours=1)
    lone = [_obs(33.0, -99.0, t, 0.9, "NOAA-20", 4)]                    # one bright pixel, one look
    big = [_obs(34.0 + 0.003 * k, -100.0, t, 0.9, "NOAA-20", 25) for k in range(4)]  # 4 px, strong FRP
    out = {round(c["lat"]): c for c in build_candidates(lone + big, [], _cells(), pack, NOW)}
    assert out[33]["route"] == "verify" and out[33]["components"]["held_back_uncorroborated"]
    assert out[34]["route"] == "priority_review"


def test_two_night_flare_is_static_in_texas():
    pack = load_pack("texas")
    flare = [_obs(31.9, -102.33, NOW - timedelta(days=d, hours=3), 0.7, "Suomi NPP", 3) for d in range(2)]
    assert build_candidates(flare, [], _cells(), pack, NOW)[0]["route"] == "likely_static"
