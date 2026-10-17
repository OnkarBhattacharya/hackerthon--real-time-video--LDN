"""Thin VSS client: login + search-and-answer + explore + suggestions + stream."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

_session = requests.Session()
_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0
_TOKEN_TTL_SEC = 50 * 60


def _cfg() -> tuple[str, str, str]:
    base = (os.environ.get("VSS_URL") or os.environ.get("INGRESS_URL") or "").rstrip("/")
    user = os.environ.get("VSS_USERNAME") or os.environ.get("USERNAME") or ""
    password = os.environ.get("VSS_PASSWORD") or os.environ.get("PASSWORD") or ""
    if not base or not user or not password:
        raise RuntimeError("VSS_URL / VSS_USERNAME / VSS_PASSWORD (or INGRESS_URL / USERNAME / PASSWORD) required")
    return base, user, password


def login(force: bool = False) -> str:
    global _token, _token_at
    with _lock:
        if not force and _token and (time.time() - _token_at) < _TOKEN_TTL_SEC:
            return _token
        base, user, password = _cfg()
        r = _session.post(
            f"{base}/api/v1/auth/login",
            json={"username": user, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        _token = r.json()["access_token"]
        _token_at = time.time()
        return _token


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {login()}", "Content-Type": "application/json"}


def _request(method: str, path: str, **kwargs: Any) -> requests.Response:
    base, _, _ = _cfg()
    url = f"{base}{path}"
    kwargs.setdefault("timeout", 120)
    r = _session.request(method, url, headers=_headers(), **kwargs)
    if r.status_code == 401:
        login(force=True)
        r = _session.request(method, url, headers=_headers(), **kwargs)
    return r


def search_and_answer(
    query: str,
    *,
    top_k: int = 10,
    min_similarity: float = 0.25,
    time_filter: str = "all",
    metadata_filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": query,
        "top_k": top_k,
        "llm_top_n": min(5, top_k),
        "min_similarity": min_similarity,
        "time_filter": time_filter,
        "include_public": True,
    }
    if metadata_filters:
        body["metadata_filters"] = metadata_filters
    r = _request("POST", "/api/v1/agent/search-and-answer", json=body)
    r.raise_for_status()
    return r.json()


def explore(limit: int = 24, offset: int = 0) -> dict[str, Any]:
    r = _request("GET", f"/api/v1/videos/explore?scope=all&limit={limit}&offset={offset}")
    r.raise_for_status()
    return r.json()


def suggestions() -> dict[str, Any]:
    r = _request("GET", "/api/v1/suggestions")
    r.raise_for_status()
    return r.json()


def stream_url(source: str) -> str:
    base, _, _ = _cfg()
    token = login()
    from urllib.parse import quote

    return f"{base}/api/v1/videos/stream?source={quote(source, safe='')}&token={quote(token, safe='')}"


def proxy_stream(source: str, range_header: str | None = None) -> requests.Response:
    base, _, _ = _cfg()
    token = login()
    from urllib.parse import quote

    url = f"{base}/api/v1/videos/stream?source={quote(source, safe='')}&token={quote(token, safe='')}"
    headers = {}
    if range_header:
        headers["Range"] = range_header
    return _session.get(url, headers=headers, stream=True, timeout=120)
