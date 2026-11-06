"""Polite HTTP for public job feeds.

- Honest User-Agent, never browser impersonation.
- A minimum delay between requests to the same host (thread-safe, so the
  discovery step can run one worker per ATS host).
- Retries with backoff on 429 / 5xx, honouring Retry-After.
- Optional on-disk cache of raw JSON responses in data/cache/http/.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache" / "http"
USER_AGENT = "job-radar/0.1 (personal job tracker; polite, cached)"
TIMEOUT = (10, 90)  # connect, read — some Lever boards are tens of MB

_local = threading.local()
_host_lock = threading.Lock()
_next_slot: dict[str, float] = {}


class FetchError(Exception):
    """A board could not be read (bad status, bad JSON, or network failure)."""


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        _local.session = s
    return _local.session


def _wait_for_host(host: str, delay: float) -> None:
    """Reserve the next free time slot for this host, then sleep until it."""
    with _host_lock:
        now = time.monotonic()
        slot = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = slot + delay
    if slot > now:
        time.sleep(slot - now)


def _cache_path(method: str, url: str, body) -> Path:
    key = f"{method} {url} {json.dumps(body, sort_keys=True) if body else ''}"
    return CACHE_DIR / f"{hashlib.sha1(key.encode()).hexdigest()}.json"


def _read_cache(path: Path, cache_seconds: int) -> str | None:
    """A fresh cached body, or None. A file that can't be read (cut off by a
    killed run, say) is a cache miss, never a crash."""
    try:
        if cache_seconds and time.time() - path.stat().st_mtime < cache_seconds:
            return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _write_cache(path: Path, text: str) -> None:
    """Write to a temp file, then rename, so readers never see half a file
    and two threads fetching the same URL can't interleave their writes."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


def request_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    delay: float = 0.5,
    cache_seconds: int = 0,
    allow_404: bool = False,
    retries: int = 3,
):
    """Fetch JSON. Returns None for a 404 when allow_404 is set."""
    path = _cache_path(method, url, body)
    cached = _read_cache(path, cache_seconds)
    if cached is not None:
        try:
            return json.loads(cached)
        except ValueError:
            pass  # damaged cache file: fetch again

    host = urlsplit(url).netloc
    last_error = "no attempt made"
    for attempt in range(retries):
        _wait_for_host(host, delay)
        try:
            resp = _session().request(method, url, json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else 2 ** (attempt + 1)
            last_error = f"HTTP {resp.status_code}"
            time.sleep(min(wait, 60))
            continue
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} for {method} {url}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise FetchError(f"Invalid JSON from {url}") from exc

        if cache_seconds:
            _write_cache(path, json.dumps(data))
        return data

    raise FetchError(f"{last_error} for {method} {url} after {retries} attempts")


def probe_status(method: str, url: str, *, body: dict | None = None, delay: float = 0.5) -> tuple[int, object]:
    """(HTTP status, JSON or None) for discovery probes that need to tell
    error codes apart, e.g. Workday's 422 (no such tenant on this pod) from
    404 (right tenant, wrong site). Same polite delay; one retry on 429/5xx."""
    host = urlsplit(url).netloc
    for attempt in range(2):
        _wait_for_host(host, delay)
        try:
            resp = _session().request(method, url, json=body, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(2 ** (attempt + 1))
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None
    raise FetchError(f"HTTP {resp.status_code} for {method} {url}")


def get_text(url: str, *, delay: float = 0.5, cache_seconds: int = 0) -> str:
    """Fetch a plain-text document (used for the SimplifyJobs README)."""
    path = _cache_path("GET-TEXT", url, None)
    cached = _read_cache(path, cache_seconds)
    if cached is not None:
        return cached
    _wait_for_host(urlsplit(url).netloc, delay)
    try:
        resp = _session().get(url, timeout=TIMEOUT, headers={"Accept": "text/plain"})
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} for GET {url}")
    if cache_seconds:
        _write_cache(path, resp.text)
    return resp.text
