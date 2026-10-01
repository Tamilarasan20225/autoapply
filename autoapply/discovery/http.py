"""
Shared HTTP layer for discovery sources.

One pooled session with retry/backoff that honours Retry-After, plus UA
rotation. Previously every source opened a fresh TCP/TLS connection per request
and only JobSpy had any retry at all.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

_RETRY_STATUSES = (429, 500, 502, 503, 504)

_session: Optional[requests.Session] = None
_session_lock = threading.Lock()

# Per-host spacing so a burst against one ATS doesn't trigger a block.
_host_last_call: dict[str, float] = {}
_host_lock = threading.Lock()
_DEFAULT_HOST_DELAY = 0.15


def random_user_agent() -> str:
    return random.choice(_USER_AGENTS)


def get_session() -> requests.Session:
    """Process-wide pooled session with retry/backoff."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is not None:
            return _session
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=2,
            read=2,
            status=3,
            backoff_factor=0.8,
            status_forcelist=_RETRY_STATUSES,
            allowed_methods=frozenset(["GET", "POST", "HEAD"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=64)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _session = session
        return _session


def _throttle(url: str, delay: float) -> None:
    if delay <= 0:
        return
    try:
        host = url.split("/", 3)[2]
    except IndexError:
        return
    with _host_lock:
        last = _host_last_call.get(host, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _host_last_call[host] = time.monotonic()


def request(
    method: str,
    url: str,
    *,
    timeout: int = 15,
    headers: Optional[dict] = None,
    host_delay: float = _DEFAULT_HOST_DELAY,
    **kwargs,
) -> Optional[requests.Response]:
    """Perform a request, returning None on transport failure (never raising)."""
    merged = {"User-Agent": random_user_agent(), "Accept": "application/json"}
    if headers:
        merged.update(headers)
    _throttle(url, host_delay)
    try:
        return get_session().request(method, url, timeout=timeout, headers=merged, **kwargs)
    except requests.RequestException:
        return None


def get(url: str, **kwargs) -> Optional[requests.Response]:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> Optional[requests.Response]:
    return request("POST", url, **kwargs)


def get_json(url: str, **kwargs) -> Optional[dict | list]:
    response = get(url, **kwargs)
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def post_json(url: str, **kwargs) -> Optional[dict | list]:
    response = post(url, **kwargs)
    if response is None or response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None
