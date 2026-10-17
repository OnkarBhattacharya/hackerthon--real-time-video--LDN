"""Thin client for live archive retrieval and the local fixture."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from backend.clips import clip_url_for_source
from backend.config import ConfigError, settings
from backend.models import Candidate, Clip, Detection

logger = logging.getLogger(__name__)

_TRANSIENT = {429, 500, 502, 503, 504}
_TOKEN_TTL_SEC = 50 * 60


class VssError(Exception):
    """A retrieval call failed. The message never includes response bodies."""

    def __init__(self, operation: str, status: int | None, hint: str) -> None:
        self.operation = operation
        self.status = status
        self.hint = hint
        label = operation if status is None else f"{operation} failed ({status})"
        super().__init__(label)


class VssClient:
    """Login once, reuse the bearer token, and retry transient failures."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_at = 0.0
        self._explore: dict[str, Any] | None = None
        self._count: int | None = None
        self._count_at = 0.0
        self._segment_numbers: dict[str, int] = {}
        self._http = httpx.Client()

    def clear_cache(self) -> None:
        """Drop cached explore data at the start of an investigation."""
        self._explore = None
        self._segment_numbers.clear()

    def list_cameras(self) -> list[str]:
        """Return indexed camera ids."""
        if settings.use_fixture():
            from backend.fixture import camera_ids

            return camera_ids()
        try:
            payload = self._request_json(
                "GET",
                "/api/v1/metadata/values?field=camera_id&limit=200",
                operation="metadata",
                detail="camera_id",
                timeout=30,
            )
        except VssError:
            logger.info("vss metadata camera_id unavailable")
            payload = {}
        found = [str(value) for value in (payload.get("values") or []) if str(value).strip()]
        if found:
            return list(dict.fromkeys(found))
        payload = self.explore(limit=100)
        found: list[str] = []
        for item in payload.get("cameras") or []:
            if isinstance(item, str) and item not in found:
                found.append(item)
            elif isinstance(item, dict) and item.get("camera_id"):
                camera_id = str(item["camera_id"])
                if camera_id not in found:
                    found.append(camera_id)
        for chunk in payload.get("chunks") or []:
            camera_id = chunk.get("camera_id")
            if camera_id and str(camera_id) not in found:
                found.append(str(camera_id))
        return found

    def indexed_segment_count(self) -> int | None:
        """Return the archive total when explore provides one."""
        if settings.use_fixture():
            from backend.fixture import CHUNKS

            return len(CHUNKS)
        now = time.time()
        if self._count is not None and (now - self._count_at) < 60:
            return self._count
        try:
            payload = self.explore(limit=1)
        except VssError:
            return None
        total = payload.get("total")
        if isinstance(total, int):
            self._count = total
            self._count_at = now
            return total
        return None

    def search(self, query: str, camera_id: str, limit: int) -> list[Candidate]:
        """Search one camera and return normalised candidates."""
        if settings.use_fixture():
            from backend.fixture import chunks_for_camera

            rows = chunks_for_camera(camera_id)[:limit]
            return [candidate_from_chunk(row, camera_id) for row in rows]
        body = {
            "query": query,
            "top_k": limit,
            "llm_top_n": 1,
            "min_similarity": 0.15,
            "time_filter": "all",
            "include_public": True,
            "metadata_filters": {"camera_id": camera_id},
        }
        payload = self._request_json(
            "POST",
            "/api/v1/search",
            operation="search",
            detail=camera_id,
            json_body=body,
            timeout=90,
        )
        rows = payload.get("results")
        if not isinstance(rows, list) or not rows:
            evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
            rows = evidence.get("chunks") or payload.get("chunks") or []
        candidates: list[Candidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate = candidate_from_chunk(row, camera_id)
            number = row.get("segment_number")
            if number is not None:
                try:
                    self._segment_numbers[candidate.segment_id] = int(number)
                except (TypeError, ValueError):
                    pass
            candidates.append(candidate)
        return candidates

    def explore(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Return one explore page. The first page is cached for neighbour lookup."""
        if limit == 100 and offset == 0 and self._explore is not None:
            return self._explore
        payload = self._request_json(
            "GET",
            f"/api/v1/videos/explore?scope=all&limit={limit}&offset={offset}",
            operation="explore",
            detail=str(limit),
            timeout=60,
        )
        if limit == 100 and offset == 0:
            self._explore = payload
        return payload

    def neighbouring_segments(
        self,
        segment_id: str,
        video_id: str,
        start: float,
        end: float,
    ) -> dict[str, Clip | None]:
        """Return the previous and next clips on the same video, when they exist."""
        if settings.use_fixture():
            from backend.fixture import neighbours_for

            return neighbours_for(segment_id)
        numbered = self._neighbours_by_segment_number(segment_id, video_id)
        if numbered is not None:
            return numbered
        previous: Clip | None = None
        nxt: Clip | None = None
        try:
            chunks = self.explore(limit=100).get("chunks") or []
        except VssError:
            logger.info("vss neighbours segment=%s status=error", segment_id)
            return {"previous": None, "next": None}
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidate = candidate_from_chunk(chunk, str(chunk.get("camera_id") or ""))
            if candidate.segment_id == segment_id or candidate.video_id != video_id:
                continue
            if not candidate.clip_url:
                continue
            clip = Clip(candidate.segment_id, candidate.start, candidate.end, candidate.clip_url)
            if candidate.end <= start and (previous is None or candidate.start >= previous.start):
                previous = clip
            if candidate.start >= end and (nxt is None or candidate.start <= nxt.start):
                nxt = clip
        return {"previous": previous, "next": nxt}

    def _neighbours_by_segment_number(
        self,
        segment_id: str,
        video_id: str,
    ) -> dict[str, Clip | None] | None:
        """Use the segment index when search stored a segment number for this hit."""
        number = self._segment_numbers.get(segment_id)
        if number is None or not video_id:
            return None
        try:
            payload = self._request_json(
                "GET",
                "/api/v1/tools/segments?original_video=" + quote(video_id, safe=""),
                operation="segments",
                detail=segment_id,
                timeout=60,
            )
        except VssError:
            logger.info("vss segments segment=%s status=error", segment_id)
            return None
        by_number: dict[int, dict[str, Any]] = {}
        for row in payload.get("segments") or []:
            if not isinstance(row, dict) or row.get("segment_number") is None:
                continue
            try:
                by_number[int(row["segment_number"])] = row
            except (TypeError, ValueError):
                continue
        return {
            "previous": clip_from_row(by_number.get(number - 1)),
            "next": clip_from_row(by_number.get(number + 1)),
        }

    def open_stream(self, source: str, range_header: str | None) -> httpx.Response:
        """Open the upstream clip stream. The URL is not logged because it carries the token."""
        token = self._bearer(force=False)
        base, _, _ = settings.require_vss()
        url = f"{base}/api/v1/videos/stream?source={quote(source, safe='')}&token={quote(token, safe='')}"
        headers = {"Range": range_header} if range_header else {}
        started = time.perf_counter()
        try:
            request = self._http.build_request("GET", url, headers=headers, timeout=120)
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.info("vss stream status=error elapsed=%.2f", time.perf_counter() - started)
            raise VssError("stream", None, "The clip service did not respond.") from exc
        logger.info(
            "vss stream status=%s elapsed=%.2f",
            response.status_code,
            time.perf_counter() - started,
        )
        if response.status_code == 401:
            response.close()
            self._bearer(force=True)
            raise VssError("stream", 401, "Login expired. Run the investigation again.")
        return response

    def _bearer(self, force: bool) -> str:
        now = time.time()
        if not force and self._token and (now - self._token_at) < _TOKEN_TTL_SEC:
            return self._token
        base, user, _password = settings.require_vss()
        started = time.perf_counter()
        try:
            response = self._http.post(
                f"{base}/api/v1/auth/login",
                json={"username": user, "password": _password},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            logger.info("vss login status=error elapsed=%.2f", time.perf_counter() - started)
            raise VssError("login", None, "Check the VSS URL and credentials.") from exc
        logger.info(
            "vss login status=%s elapsed=%.2f",
            response.status_code,
            time.perf_counter() - started,
        )
        if response.status_code >= 400:
            raise VssError("login", response.status_code, "Check the username and password.")
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise VssError("login", response.status_code, "Login did not return an access token.")
        self._token = token
        self._token_at = time.time()
        return token

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        detail: str,
        json_body: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        refreshed = False
        for attempt in range(3):
            try:
                token = self._bearer(force=False)
                base, _, _ = settings.require_vss()
                response = self._http.request(
                    method,
                    f"{base}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=json_body,
                    timeout=timeout,
                )
            except ConfigError:
                raise
            except VssError:
                raise
            except httpx.HTTPError:
                if attempt < 2:
                    time.sleep(0.4 * (2**attempt))
                    continue
                logger.info(
                    "vss %s detail=%s status=error elapsed=%.2f",
                    operation,
                    detail,
                    time.perf_counter() - started,
                )
                raise VssError(operation, None, "The retrieval service did not respond.")
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self._bearer(force=True)
                continue
            if response.status_code in _TRANSIENT and attempt < 2:
                time.sleep(0.4 * (2**attempt))
                continue
            logger.info(
                "vss %s detail=%s status=%s elapsed=%.2f",
                operation,
                detail,
                response.status_code,
                time.perf_counter() - started,
            )
            if response.status_code >= 400:
                raise VssError(
                    operation,
                    response.status_code,
                    "Check login, then the dashboard, then valid metadata values.",
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise VssError(operation, response.status_code, "The retrieval service returned an unexpected payload.")
            return payload
        raise VssError(operation, None, "The retrieval service did not respond.")


def candidate_from_chunk(chunk: dict[str, Any], camera_id: str) -> Candidate:
    """Map one search or explore chunk onto a Candidate. Missing fields stay empty."""
    start = _number(
        _first(chunk, "start", "start_sec", "best_match_start_sec", "segment_start_sec"),
        0,
    )
    end = _number(
        _first(chunk, "end", "end_sec", "best_match_end_sec", "segment_end_sec"),
        start,
    )
    video_id = str(_first(chunk, "video_id", "original_video", "filename") or "")
    resolved_camera = str(_first(chunk, "camera_id") or camera_id)
    segment_id = str(
        _first(chunk, "segment_id", "id", "chunk_id", "source")
        or f"{resolved_camera}:{video_id}:{start}:{end}"
    )
    preview = _first(chunk, "preview_source", "source")
    return Candidate(
        segment_id=segment_id,
        video_id=video_id,
        camera_id=resolved_camera,
        location=str(_first(chunk, "location") or ""),
        start=start,
        end=end,
        description=str(_first(chunk, "description", "reasoning_content", "caption") or ""),
        detections=_detections(chunk),
        search_score=_number(_first(chunk, "search_score", "similarity_score", "score"), 0),
        clip_url=clip_url_for_source(str(preview) if preview else None) or "",
    )


def clip_from_row(row: dict[str, Any] | None) -> Clip | None:
    """Map one neighbour segment row onto a playable clip."""
    if not isinstance(row, dict):
        return None
    source = _first(row, "preview_source", "source")
    url = clip_url_for_source(str(source) if source else None)
    if not url:
        return None
    start = _number(_first(row, "start", "start_sec", "segment_start_sec"), 0)
    end = _number(_first(row, "end", "end_sec", "segment_end_sec"), start)
    segment_id = str(_first(row, "segment_id", "source") or "")
    return Clip(segment_id, start, end, url)


def _first(chunk: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = chunk.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _detections(chunk: dict[str, Any]) -> list[Detection]:
    """Read a detections list, or object_counts when search returns metadata strings."""
    raw = chunk.get("detections")
    if isinstance(raw, list) and raw:
        detections: list[Detection] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = str(item.get("class") or item.get("label") or "")
            boxes = item.get("boxes")
            if boxes is None:
                boxes = item.get("bbox")
            detections.append(
                Detection(label=label, count=int(item.get("count") or 1), boxes=boxes)
            )
        if detections:
            return detections
    counts = _coerce_counts(chunk.get("object_counts"))
    if counts:
        return [Detection(label=label, count=count, boxes=None) for label, count in counts.items()]
    return [
        Detection(label=label, count=1, boxes=None)
        for label in _coerce_classes(chunk.get("object_classes"))
    ]


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
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            counts[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return counts


def _coerce_classes(value: Any) -> list[str]:
    """Accept a class list or a comma-separated string."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


_client: VssClient | None = None


def get_client() -> VssClient:
    """Return the process-wide retrieval client."""
    global _client
    if _client is None:
        _client = VssClient()
    return _client
