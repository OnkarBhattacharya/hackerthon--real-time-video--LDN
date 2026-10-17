"""Thin VSS retrieval client (login, search, segment, clip, neighbours)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import requests

from backend.config import Settings, load_settings
from backend.models import Candidate, Detection, DetectionBox

log = logging.getLogger(__name__)


class VssAuthError(RuntimeError):
    """Login or token failure."""


class VssRequestError(RuntimeError):
    """Non-auth VSS HTTP failure after retries."""


class VssClient:
    """JWT-authenticated client for the team VSS retrieval API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._session = requests.Session()
        self._token: str | None = None
        self._token_at = 0.0
        self._token_ttl = 50 * 60

    # ---- auth ----

    def login(self, *, force: bool = False) -> str:
        """Obtain a bearer token; reuse until TTL unless force=True."""
        if (
            not force
            and self._token
            and (time.time() - self._token_at) < self._token_ttl
        ):
            return self._token
        url = f"{self.settings.vss_url}/api/v1/auth/login"
        log.info("vss.login url=%s user=%s", self.settings.vss_url, self.settings.vss_username)
        try:
            r = self._session.post(
                url,
                json={
                    "username": self.settings.vss_username,
                    "password": self.settings.vss_password,
                },
                timeout=self.settings.request_timeout_sec,
            )
        except requests.RequestException as exc:
            raise VssAuthError(f"login request failed: {exc}") from exc
        if r.status_code >= 400:
            raise VssAuthError(f"login HTTP {r.status_code}")
        token = r.json().get("access_token")
        if not token:
            raise VssAuthError("login response missing access_token")
        self._token = token
        self._token_at = time.time()
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.login()}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        url = f"{self.settings.vss_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.settings.max_retries):
            try:
                r = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                    timeout=self.settings.request_timeout_sec,
                    stream=stream,
                )
                if r.status_code == 401 and attempt + 1 < self.settings.max_retries:
                    self.login(force=True)
                    continue
                if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < self.settings.max_retries:
                    delay = 0.5 * (2**attempt)
                    log.warning(
                        "vss.retry method=%s path=%s status=%s sleep=%.1fs",
                        method,
                        path,
                        r.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                return r
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= self.settings.max_retries:
                    break
                delay = 0.5 * (2**attempt)
                log.warning(
                    "vss.retry method=%s path=%s error=%s sleep=%.1fs",
                    method,
                    path,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
        raise VssRequestError(f"{method} {path} failed after retries: {last_exc}")

    # ---- metadata / catalog ----

    def list_camera_ids(self) -> list[str]:
        """Return distinct camera_id values from metadata/values."""
        r = self._request("GET", "/api/v1/metadata/values", params={"field": "camera_id", "limit": 200})
        if r.status_code >= 400:
            raise VssRequestError(f"metadata/values camera_id HTTP {r.status_code}")
        values = r.json().get("values") or []
        return [str(v) for v in values]

    # ---- search ----

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        min_similarity: float | None = None,
        camera_id: str | None = None,
        metadata_filters: dict[str, Any] | None = None,
        time_filter: str = "all",
    ) -> list[dict[str, Any]]:
        """Hybrid search; returns results[] segment rows."""
        filters = dict(metadata_filters or {})
        if camera_id:
            filters["camera_id"] = camera_id
        body: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "llm_top_n": 1,
            "min_similarity": (
                self.settings.min_similarity if min_similarity is None else min_similarity
            ),
            "time_filter": time_filter,
            "include_public": True,
        }
        if filters:
            body["metadata_filters"] = filters
        log.info(
            "vss.search query=%r camera_id=%s top_k=%s",
            query[:80],
            camera_id,
            top_k,
        )
        r = self._request("POST", "/api/v1/search", json_body=body)
        if r.status_code >= 400:
            raise VssRequestError(f"search HTTP {r.status_code}: {r.text[:200]}")
        return list(r.json().get("results") or [])

    # ---- segment details ----

    def segment_metadata(self, source: str) -> dict[str, Any]:
        r = self._request(
            "GET",
            "/api/v1/videos/metadata",
            params={"source": source},
        )
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise VssRequestError(f"metadata HTTP {r.status_code}")
        return r.json()

    def segment_detections(self, source: str) -> dict[str, Any]:
        r = self._request(
            "GET",
            "/api/v1/videos/detections",
            params={"source": source},
        )
        if r.status_code == 404:
            return {}
        if r.status_code >= 400:
            raise VssRequestError(f"detections HTTP {r.status_code}")
        return r.json()

    def clip_url(self, source: str) -> str:
        """Build a browser-playable stream URL (JWT as query param)."""
        token = self.login()
        return (
            f"{self.settings.vss_url}/api/v1/videos/stream"
            f"?source={quote(source, safe='')}&token={quote(token, safe='')}"
        )

    def neighbouring_segments(
        self, original_video: str, segment_number: int
    ) -> dict[str, dict[str, Any] | None]:
        """Return prev/current/next segment rows for the same parent video."""
        r = self._request(
            "GET",
            "/api/v1/tools/segments",
            params={"original_video": original_video},
        )
        if r.status_code >= 400:
            raise VssRequestError(f"tools/segments HTTP {r.status_code}")
        data = r.json()
        segments = list(data.get("segments") or [])
        by_num = {
            int(s["segment_number"]): s
            for s in segments
            if s.get("segment_number") is not None
        }
        return {
            "previous": by_num.get(segment_number - 1),
            "current": by_num.get(segment_number),
            "next": by_num.get(segment_number + 1),
        }

    # ---- normalisation ----

    def row_to_candidate(self, row: dict[str, Any], *, enrich_boxes: bool = False) -> Candidate:
        """Map a search/metadata row into a Candidate."""
        source = str(row.get("source") or "")
        if not source:
            raise ValueError("row missing source")
        detections = _detections_from_row(row)
        if enrich_boxes:
            det_payload = self.segment_detections(source)
            detections = _merge_detection_boxes(detections, det_payload)
        return Candidate(
            segment_id=source,
            video_id=str(row.get("original_video") or ""),
            camera_id=str(row.get("camera_id") or "unknown"),
            location=str(row.get("location") or ""),
            start=float(row.get("segment_start_sec") or 0.0),
            end=float(row.get("segment_end_sec") or 0.0),
            description=str(row.get("reasoning_content") or ""),
            detections=detections,
            search_score=float(row.get("similarity_score") or 0.0),
            clip_url=self.clip_url(source),
            segment_number=(
                int(row["segment_number"]) if row.get("segment_number") is not None else None
            ),
            total_segments=(
                int(row["total_segments"]) if row.get("total_segments") is not None else None
            ),
            filename=row.get("filename"),
            capture_type=row.get("capture_type"),
        )


def _detections_from_row(row: dict[str, Any]) -> list[Detection]:
    """Normalise object_counts / object_classes (dict, list, or JSON/CSV string)."""
    counts = _coerce_counts(row.get("object_counts"))
    if counts:
        return [Detection(class_name=cls, count=n) for cls, n in counts.items()]
    return [
        Detection(class_name=cls, count=1)
        for cls in _coerce_classes(row.get("object_classes"))
    ]


def _merge_detection_boxes(
    base: list[Detection], payload: dict[str, Any]
) -> list[Detection]:
    if not payload:
        return base
    by_class: dict[str, Detection] = {d.class_name: Detection(d.class_name, d.count, list(d.boxes)) for d in base}
    frames = payload.get("frames") or []
    for frame in frames:
        fi = frame.get("frame_index")
        for det in frame.get("detections") or []:
            cls = str(det.get("class") or det.get("label") or "object")
            box = det.get("bbox") or det.get("box") or det
            try:
                x1 = float(box.get("x1", box.get("xmin", 0)))
                y1 = float(box.get("y1", box.get("ymin", 0)))
                x2 = float(box.get("x2", box.get("xmax", 0)))
                y2 = float(box.get("y2", box.get("ymax", 0)))
            except (TypeError, AttributeError, ValueError):
                continue
            entry = by_class.setdefault(cls, Detection(class_name=cls, count=0))
            entry.boxes.append(
                DetectionBox(
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                    conf=(float(det["conf"]) if det.get("conf") is not None else None),
                    frame_index=int(fi) if fi is not None else None,
                )
            )
            entry.count = max(entry.count, len(entry.boxes))
    for cls, n in _coerce_counts(payload.get("object_counts")).items():
        entry = by_class.setdefault(cls, Detection(class_name=cls, count=0))
        entry.count = n
    return list(by_class.values())


def _coerce_counts(value: Any) -> dict[str, int]:
    """Accept a count dict or a JSON object string from search metadata."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, count in value.items():
        try:
            out[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _coerce_classes(value: Any) -> list[str]:
    """Accept a class list or a comma-separated string."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []
