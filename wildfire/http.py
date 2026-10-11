"""Hardened outbound HTTP for provider adapters.

* Only hosts on an explicit allowlist can be reached (no SSRF via config).
* TLS verification always on; bounded timeouts; bounded response size.
* Retries with exponential backoff on 429/5xx only.
* Secrets (e.g. FIRMS MAP_KEY) are redacted from every log line and from
  any URL that ends up in provenance metadata.
"""
from __future__ import annotations

import logging
import os
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("wildfire.http")

ALLOWED_HOSTS = {
    "firms.modaps.eosdis.nasa.gov",
    "api.open-meteo.com",
    "services3.arcgis.com",
}
MAX_BYTES = 60 * 1024 * 1024
USER_AGENT = "wildfire-intel/0.3 (+https://github.com/; decision-support research)"
_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


class FetchError(RuntimeError):
    pass


_TRANSPORT = None   # tests inject an httpx.MockTransport here
# iter_bytes() already undoes gzip/deflate/br, so these headers must not travel
# with the decoded body or httpx would try to decompress it a second time.
_HOP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def get(url: str, *, params: dict | None = None, timeout: float = 60.0, retries: int = 3) -> httpx.Response:
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise FetchError(f"blocked outbound request to {host!r} (not on allowlist)")
    delay = 2.0
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT},
                              follow_redirects=False, verify=True, transport=_TRANSPORT) as c:
                with c.stream("GET", url, params=params) as r:
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise FetchError(f"response from {host} exceeded {MAX_BYTES} bytes")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                headers = [(k, v) for k, v in r.headers.items() if k.lower() not in _HOP_HEADERS]
                resp = httpx.Response(r.status_code, headers=headers, content=body, request=r.request)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                log.warning("%s returned %s; retry %d", host, resp.status_code, attempt)
                wait = delay
                if resp.status_code == 429:   # per-minute quotas: wait out the window
                    ra = resp.headers.get("retry-after", "")
                    wait = min(90.0, float(ra)) if ra.replace(".", "", 1).isdigit() else 61.0
                time.sleep(wait)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise FetchError(f"{host} returned HTTP {resp.status_code}: {redact(resp.text[:200])}")
            return resp
        except httpx.DecodingError as e:
            raise FetchError(f"{host} sent a body that could not be decoded: {e}") from e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            log.warning("%s transport error (%s); attempt %d/%d", host, type(e).__name__, attempt, retries)
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
    raise FetchError(f"{host} unreachable after {retries} attempts: {redact(str(last))}")


def env_secret(name: str) -> str | None:
    v = os.environ.get(name, "").strip() or None
    register_secret(v)
    return v
