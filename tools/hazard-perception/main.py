#!/usr/bin/env python3
"""Hazard-perception test. Proxied by hackathon-app at /hazard.

One main hazard is boxed on the second pass. The clip search follows the
mode the tester picked, and the first pass does not wait on detections.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:  # self-check on a machine that does not have Flask
    class _Map:
        strict_slashes = True

    class _Request:
        method = "GET"
        args: dict[str, str] = {}
        headers: dict[str, str] = {}

        def get_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {}

    class _Flask:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.url_map = _Map()

        def route(self, *args: Any, **kwargs: Any):
            def deco(fn):
                return fn
            return deco

        def get(self, *args: Any, **kwargs: Any):
            return self.route(*args, **kwargs)

        def post(self, *args: Any, **kwargs: Any):
            return self.route(*args, **kwargs)

        def run(self, *args: Any, **kwargs: Any) -> None:
            return None

    def Flask(*args: Any, **kwargs: Any) -> _Flask:
        return _Flask(*args, **kwargs)

    class Response:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    def jsonify(payload: Any) -> Any:
        return payload

    request = _Request()

    def stream_with_context(fn: Any) -> Any:
        return fn

PORT = int(os.environ.get("PORT", "8080"))

ACTIONS = [
    {"id": "slow", "key": "1", "label": "Slow down"},
    {"id": "brake", "key": "2", "label": "Brake"},
    {"id": "wait", "key": "3", "label": "Wait"},
    {"id": "steer", "key": "4", "label": "Steer away"},
    {"id": "continue", "key": "5", "label": "Continue"},
]
ACTION_LABELS = {row["id"]: row["label"] for row in ACTIONS}

# One-line queries. Driving never uses the warehouse camera, and the
# warehouse modes never use a street or traffic filter.
MODES: dict[str, dict[str, Any]] = {
    "driving": {
        "name": "Driving",
        "query": "person or cyclist stepping out near cars in street traffic",
        "filters": (
            {"capture_type": "streets"},
            {"capture_type": "traffic"},
        ),
    },
    "forklift": {
        "name": "Forklift driver",
        "query": "forklift driver approaching a person or a rack in the aisle",
        "filters": (
            {
                "location": "warehouse3",
                "camera_id": "sdg_warehouse_cam-2",
                "capture_type": "warehouse",
            },
        ),
    },
    "walk": {
        "name": "Warehouse walk",
        "query": "person on foot with a load while a forklift comes toward them",
        "filters": (
            {
                "location": "warehouse3",
                "camera_id": "sdg_warehouse_cam-2",
                "capture_type": "warehouse",
            },
        ),
    },
}

WAREHOUSE_FILTER = {
    "location": "warehouse3",
    "camera_id": "sdg_warehouse_cam-2",
    "capture_type": "warehouse",
}

# Score for the chosen reaction against the one hazard we boxed.
# Cosmos Reason is not called live; this table is the judge.
RULES: dict[str, tuple[dict[str, int], str]] = {
    "person stepping out": (
        {"brake": 5, "slow": 3, "wait": 2, "steer": 1, "continue": 0},
        "A person is stepping into your path, so brake.",
    ),
    "cyclist": (
        {"brake": 5, "slow": 4, "steer": 3, "wait": 2, "continue": 0},
        "A cyclist is close, so brake or slow down.",
    ),
    "motorcycle": (
        {"brake": 5, "slow": 4, "steer": 3, "wait": 2, "continue": 0},
        "A motorcycle is close, so brake or slow down.",
    ),
    "close car": (
        {"slow": 5, "brake": 4, "steer": 2, "wait": 1, "continue": 0},
        "A car is close, so slow down.",
    ),
    "close truck": (
        {"slow": 5, "brake": 4, "steer": 2, "wait": 1, "continue": 0},
        "A truck is close, so slow down.",
    ),
    "bus": (
        {"slow": 5, "brake": 4, "wait": 2, "steer": 1, "continue": 0},
        "A bus is close, so slow down.",
    ),
    "person in the aisle": (
        {"brake": 5, "wait": 4, "slow": 3, "steer": 1, "continue": 0},
        "A person is in the aisle, so brake and let them clear.",
    ),
    "another vehicle": (
        {"slow": 5, "brake": 4, "wait": 3, "steer": 2, "continue": 0},
        "Another vehicle is close, so slow down.",
    ),
    "rack ahead": (
        {"slow": 5, "brake": 4, "steer": 3, "wait": 2, "continue": 0},
        "The forks are approaching a rack, so slow down.",
    ),
    "forklift": (
        {"wait": 5, "steer": 4, "slow": 2, "brake": 1, "continue": 0},
        "A forklift is coming, so wait and let it pass.",
    ),
    "person with a load": (
        {"wait": 5, "slow": 4, "steer": 3, "brake": 1, "continue": 0},
        "A person is carrying a load, so wait and give them room.",
    ),
}

VEHICLES = {"car", "bus", "truck"}
BIKES = {"bicycle", "motorcycle"}
RACKS = {"refrigerator", "dining table"}
EARLY_SEC = 1.0


def _resolve_vss_url() -> str:
    raw = (
        os.environ.get("VSS_INTERNAL_URL")
        or os.environ.get("VSS_URL")
        or "http://video-backend-service:8000"
    ).rstrip("/")
    host = raw.split("://")[-1].split("/")[0].split(":")[0]
    if host.endswith(".cosmos.vastdata.com") or host.startswith("video-lab-team-"):
        return "http://video-backend-service:8000"
    return raw


VSS_URL = _resolve_vss_url()
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")

app = Flask(__name__)
app.url_map.strict_slashes = False

_token_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0
_clip_lock = threading.Lock()
_clips: dict[str, dict[str, Any]] = {}
_det_lock = threading.Lock()
_det_cache: dict[str, dict[str, Any] | None] = {}
_hazard_lock = threading.Lock()
_hazard_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_seq = 0


def _login(force: bool = False) -> str:
    global _token, _token_at
    with _token_lock:
        if not force and _token and (time.time() - _token_at) < 1500:
            return _token
        if not VSS_URL or not VSS_USERNAME or not VSS_PASSWORD:
            raise RuntimeError("VSS credentials are not configured")
        response = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
        )
        response.raise_for_status()
        _token = response.json()["access_token"]
        _token_at = time.time()
        return _token


def _vss(method: str, path: str, **kwargs: Any) -> requests.Response:
    token = _login()
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    timeout = kwargs.pop("timeout", 40)
    response = requests.request(
        method, f"{VSS_URL}{path}", headers=headers, timeout=timeout, **kwargs
    )
    if response.status_code == 401:
        token = _login(force=True)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.request(
            method, f"{VSS_URL}{path}", headers=headers, timeout=timeout, **kwargs
        )
    return response


def _place(value: str) -> str:
    text = (value or "").replace("_", " ").strip()
    return text.title() if text else "Clip"


def _search_body(query: str, filters: dict[str, str], top_k: int = 8) -> dict[str, Any]:
    if "\n" in query or not query.strip():
        raise ValueError("search query must be one line")
    return {
        "query": query,
        "top_k": max(1, min(100, int(top_k))),
        "llm_top_n": 1,
        "min_similarity": 0.15,
        "include_public": True,
        "time_filter": "all",
        "metadata_filters": filters,
    }


def _allows(mode: str, chunk: dict[str, Any]) -> bool:
    """Keep a driving clip off the forklift camera, and the reverse."""
    loc = str(chunk.get("location") or "")
    cam = str(chunk.get("camera_id") or "")
    cap = str(chunk.get("capture_type") or "")
    if mode == "driving":
        if loc == "warehouse3" or cam == "sdg_warehouse_cam-2" or cap == "warehouse":
            return False
        if cap not in {"streets", "traffic"}:
            return False
        # Helmet-cam riding is not a driving test.
        if cam == "nyc_bike_gopro-1":
            return False
        return True
    return loc == "warehouse3" and cam == "sdg_warehouse_cam-2" and cap == "warehouse"


def _rank(mode: str, chunk: dict[str, Any], row: dict[str, Any]) -> float:
    score = float(chunk.get("similarity_score") or 0)
    text = str(chunk.get("reasoning_content") or "").lower()
    filename = str(chunk.get("preview_source") or "").lower()
    classes = str(row.get("object_classes") or "")
    if mode == "driving":
        if any(name in classes for name in ("person", "bicycle", "motorcycle")):
            score += 0.12
        if chunk.get("capture_type") == "streets":
            score += 0.04
        if any(word in text for word in ("crosswalk", "pedestrian", "stepping", "cyclist")):
            score += 0.03
        return score
    if mode == "forklift":
        if any(word in text for word in ("aisle", "rack", "shelf", "forks", "fork")):
            score += 0.1
        if "walks away" in text or "walking away" in text:
            score -= 0.15
        if ".eye_" in filename:
            score += 0.05
        if "ceiling" in filename:
            score -= 0.06
        return score
    if any(word in text for word in ("walk", "walking", "on foot")):
        score += 0.1
    if any(word in text for word in ("load", "carrying", "suitcase", "pallet")):
        score += 0.06
    counts = str(row.get("object_counts") or "")
    if "suitcase" in counts or "backpack" in counts:
        score += 0.05
    if any(word in text for word in ("toward", "comes", "approaches", "moving")):
        score += 0.03
    if "ceiling" in filename:
        score += 0.04
    if ".eye_" in filename and "forks" in text:
        score -= 0.08
    return score


_CHUNK_SUFFIX = re.compile(r"_chunk_\d+(?=\.[^.]+$)", re.IGNORECASE)


def _video_identity(source: str, original: str = "") -> str:
    """Parent video, so chunk 0000 and chunk 0004 of one upload count as one clip."""
    basis = (original or "").strip()
    if not basis.startswith("s3://"):
        basis = (source or "").strip()
    name = basis.rsplit("/", 1)[-1]
    return _CHUNK_SUFFIX.sub("", name).strip().lower()


def _blocked_videos(avoid: set[str]) -> set[str]:
    """Hard block. A 0.2 score penalty left the same top hit in first place."""
    blocked: set[str] = set()
    for source in avoid:
        text = (source or "").strip()
        if not text:
            continue
        blocked.add(text)
        ident = _video_identity(text)
        if ident:
            blocked.add(ident)
    with _clip_lock:
        for row in _clips.values():
            if row.get("source") in avoid or row.get("video") in avoid:
                key = str(row.get("video_key") or "")
                if key:
                    blocked.add(key)
    return blocked


def _same_video(source: str, original: str, blocked: set[str]) -> bool:
    if source in blocked or (original and original in blocked):
        return True
    ident = _video_identity(source, original)
    return bool(ident and ident in blocked)


def _best_chunk(
    mode: str, payload: dict[str, Any], avoid: set[str]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Highest ranked chunk whose parent video has not already been played."""
    rows = {
        row.get("source"): row
        for row in (payload.get("results") or [])
        if isinstance(row, dict)
    }
    blocked = _blocked_videos(avoid)
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_score = -1e9
    for chunk in payload.get("chunk_results") or []:
        if not isinstance(chunk, dict):
            continue
        source = str(chunk.get("preview_source") or "")
        original = str(chunk.get("original_video") or "")
        if not source.startswith("s3://"):
            continue
        if not _allows(mode, chunk):
            continue
        if _same_video(source, original, blocked):
            continue
        row = rows.get(source) or {}
        score = _rank(mode, chunk, row)
        if score > best_score:
            best_score = score
            best = (chunk, row)
    return best


def _search(query: str, filters: dict[str, str], top_k: int = 8) -> dict[str, Any]:
    response = _vss("POST", "/api/v1/search", json=_search_body(query, filters, top_k), timeout=50)
    if response.status_code >= 400:
        raise RuntimeError(f"search HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("search returned an unexpected payload")
    return payload


def _seen_sources(raw: Any) -> set[str]:
    """Sources this browser session has already played. Repeats are excluded."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    seen: set[str] = set()
    for item in raw[:40]:
        text = str(item or "").strip()
        if text.startswith("s3://"):
            seen.add(text)
    return seen


def _load_clip(mode: str, seen: set[str] | None = None) -> dict[str, Any]:
    """Search only. Detections wait until the second pass.

    Played s3 sources are hard-excluded, including other segments of the same
    parent. top_k widens while every returned hit is a video already played.
    """
    spec = MODES[mode]
    avoid = set(seen or ())
    found: tuple[dict[str, Any], dict[str, Any]] | None = None
    used_filters: dict[str, str] | None = None
    distinct: set[str] = set()
    top_k = 8
    while True:
        filled = False
        distinct = set()
        found = None
        used_filters = None
        for filters in spec["filters"]:
            payload = _search(spec["query"], filters, top_k)
            hits = 0
            for chunk in payload.get("chunk_results") or []:
                if not isinstance(chunk, dict):
                    continue
                source = str(chunk.get("preview_source") or "")
                if not source.startswith("s3://") or not _allows(mode, chunk):
                    continue
                hits += 1
                ident = _video_identity(source, str(chunk.get("original_video") or ""))
                if ident:
                    distinct.add(ident)
            if hits >= top_k:
                filled = True
            pick = _best_chunk(mode, payload, avoid)
            if pick:
                found = pick
                used_filters = filters
                break
        if found or not filled or top_k >= 100:
            break
        top_k = min(100, top_k * 4)
    if not found or used_filters is None:
        if avoid and len(distinct) <= 1:
            raise RuntimeError("This mode only has one eligible clip in the archive.")
        if avoid:
            raise RuntimeError("Every clip for this mode was already used in this session")
        if mode == "driving":
            raise RuntimeError("No street or traffic clip is indexed for this mode")
        raise RuntimeError("No warehouse3 clip is indexed for this mode")
    chunk, row = found
    source = str(chunk.get("preview_source") or "")
    original = str(chunk.get("original_video") or "")
    video = original if original.startswith("s3://") else source
    try:
        duration = float(row.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        try:
            start = float(chunk.get("best_match_start_sec") or 0)
            end = float(chunk.get("best_match_end_sec") or 0)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        duration = end - start if end > start else 5.0
    global _seq
    with _clip_lock:
        _seq += 1
        clip_id = f"{mode}-{_seq}"
        stored = {
            "id": clip_id,
            "mode": mode,
            "mode_name": spec["name"],
            "query": spec["query"],
            "filters": used_filters,
            "place": _place(str(chunk.get("location") or "")),
            "location": str(chunk.get("location") or ""),
            "camera_id": str(chunk.get("camera_id") or ""),
            "capture_type": str(chunk.get("capture_type") or ""),
            "source": source,
            "video": video,
            "video_key": _video_identity(source, original),
            "duration": round(max(1.5, duration), 2),
            "stream": "api/stream?source=" + quote(source, safe=""),
        }
        _clips[clip_id] = stored
        return stored


def _public_clip(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "mode": row["mode"],
        "mode_name": row["mode_name"],
        "place": row["place"],
        "source": row["source"],
        "video": row.get("video") or row["source"],
        "stream": row["stream"],
        "duration": row["duration"],
    }


def _detections(source: str) -> dict[str, Any] | None:
    """Return the YOLO sidecar, or None when the segment has no sidecar."""
    with _det_lock:
        if source in _det_cache:
            return _det_cache[source]
    response = _vss("GET", "/api/v1/videos/detections", params={"source": source}, timeout=60)
    if response.status_code == 404:
        data: dict[str, Any] | None = None
    elif response.status_code >= 400:
        raise RuntimeError(f"detections HTTP {response.status_code}")
    else:
        payload = response.json()
        data = payload if isinstance(payload, dict) else None
    with _det_lock:
        _det_cache[source] = data
    return data


def _norm_box(
    det: dict[str, Any], width: float, height: float
) -> tuple[float, float, float, float] | None:
    raw = det.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        conf = float(det.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    if conf < 0.45 or width <= 0 or height <= 0:
        return None
    x = x1 / width
    y = y1 / height
    w = (x2 - x1) / width
    h = (y2 - y1) / height
    if w <= 0.005 or h <= 0.005:
        return None
    return x, y, w, h


def _classify(mode: str, label: str, box: tuple[float, float, float, float]) -> tuple[int, str] | None:
    """Priority: person in the path, then a bicycle or motorcycle, then a close vehicle.

    Distant cars, traffic lights, benches, and clocks never qualify.
    """
    x, y, w, h = box
    cx = x + w / 2
    cy = y + h / 2
    path_x = (0.28, 0.72) if mode == "driving" else (0.18, 0.82)
    in_path = path_x[0] <= cx <= path_x[1] and cy >= 0.36 and h >= 0.10
    close_vehicle = h >= 0.15 or (h >= 0.12 and w >= 0.12)
    distant_vehicle = label in VEHICLES and h < 0.12 and w < 0.10
    if distant_vehicle:
        return None
    if mode == "driving":
        if label == "person" and in_path:
            return 0, "person stepping out"
        if label == "bicycle" and (h >= 0.06 or w >= 0.07):
            return 1, "cyclist"
        if label == "motorcycle" and (h >= 0.06 or w >= 0.07):
            return 1, "motorcycle"
        if label in VEHICLES and close_vehicle:
            return 2, {"car": "close car", "truck": "close truck", "bus": "bus"}[label]
        return None
    if mode == "forklift":
        if label == "person" and (in_path or (h >= 0.12 and path_x[0] <= cx <= path_x[1])):
            return 0, "person in the aisle"
        if label in BIKES and (h >= 0.06 or w >= 0.07):
            return 1, "cyclist" if label == "bicycle" else "motorcycle"
        if label in VEHICLES and close_vehicle:
            return 2, "another vehicle"
        if label in RACKS and h >= 0.10 and path_x[0] <= cx <= path_x[1]:
            return 3, "rack ahead"
        return None
    if label == "person" and (in_path or (h >= 0.12 and path_x[0] <= cx <= path_x[1])):
        return 0, "person with a load"
    if label in BIKES and (h >= 0.06 or w >= 0.07):
        return 1, "cyclist" if label == "bicycle" else "motorcycle"
    if label in VEHICLES and close_vehicle:
        return 2, "forklift"
    if label in RACKS and (h >= 0.12 or w >= 0.12):
        return 2, "forklift"
    return None


def _frame_shape(frame: dict[str, Any], data: dict[str, Any]) -> tuple[float, float]:
    shape = frame.get("shape") or data.get("video_shape") or [1080, 1920]
    try:
        height, width = float(shape[0]), float(shape[1])
    except (TypeError, ValueError, IndexError):
        return 0.0, 0.0
    return height, width


def _candidates(
    mode: str, data: dict[str, Any], t0: float, t1: float
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for frame in data.get("frames") or []:
        try:
            moment = float(frame.get("time_sec") or 0)
        except (TypeError, ValueError):
            continue
        if moment < t0 or moment > t1:
            continue
        height, width = _frame_shape(frame, data)
        if width <= 0 or height <= 0:
            continue
        for det in frame.get("detections") or []:
            if not isinstance(det, dict):
                continue
            label = str(det.get("label") or "")
            box = _norm_box(det, width, height)
            if box is None:
                continue
            ranked = _classify(mode, label, box)
            if ranked is None:
                continue
            tier, display = ranked
            x, y, w, h = box
            try:
                conf = float(det.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            found.append(
                {
                    "tier": tier,
                    "label": display,
                    "raw": label,
                    "t": moment,
                    "h": h,
                    "conf": conf,
                    "cx": x + w / 2,
                    "cy": y + h / 2,
                    "box": {
                        "label": display,
                        "x": round(x, 4),
                        "y": round(y, 4),
                        "w": round(w, 4),
                        "h": round(h, 4),
                    },
                }
            )
    return found


def _pick_winner(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cands:
        return None
    return min(cands, key=lambda row: (row["tier"], -row["h"], -row["conf"], row["t"]))


def _track(mode: str, data: dict[str, Any], winner: dict[str, Any]) -> list[dict[str, Any]]:
    """Follow that one object and emit a single box per frame."""
    t_win = float(winner["t"])
    frames_out: list[dict[str, Any]] = []
    last_t = -1.0
    for frame in data.get("frames") or []:
        try:
            moment = float(frame.get("time_sec") or 0)
        except (TypeError, ValueError):
            continue
        if abs(moment - t_win) > 1.25:
            continue
        height, width = _frame_shape(frame, data)
        if width <= 0 or height <= 0:
            continue
        best: tuple[float, dict[str, Any]] | None = None
        for det in frame.get("detections") or []:
            if not isinstance(det, dict) or str(det.get("label") or "") != winner["raw"]:
                continue
            box = _norm_box(det, width, height)
            if box is None:
                continue
            x, y, w, h = box
            dist = abs((x + w / 2) - winner["cx"]) + abs((y + h / 2) - winner["cy"])
            if dist > 0.28:
                continue
            ranked = _classify(mode, winner["raw"], box)
            if ranked is None:
                continue
            if best is None or dist < best[0]:
                best = (
                    dist,
                    {
                        "label": winner["label"],
                        "x": round(x, 4),
                        "y": round(y, 4),
                        "w": round(w, 4),
                        "h": round(h, 4),
                    },
                )
        if best is None or moment - last_t < 0.08:
            continue
        last_t = moment
        frames_out.append({"t": round(moment, 3), "boxes": [best[1]]})
    if not frames_out:
        frames_out.append({"t": round(t_win, 3), "boxes": [winner["box"]]})
    return frames_out


def _click_times(payload: dict[str, Any], duration: float) -> list[float]:
    times: list[float] = []
    for raw in payload.get("clicks") or []:
        if isinstance(raw, dict):
            raw = raw.get("t")
        try:
            moment = float(raw)
        except (TypeError, ValueError):
            continue
        if moment < 0 or moment > duration + 1.5:
            continue
        times.append(moment)
        if len(times) >= 12:
            break
    return times


def _main_hazard(
    mode: str,
    data: dict[str, Any] | None,
    clicks: list[float],
    duration: float,
) -> dict[str, Any]:
    duration = max(1.5, duration)
    if not data or not isinstance(data.get("frames"), list):
        return {
            "sidecar": False,
            "label": None,
            "time": None,
            "frames": [],
            "note": "YOLO had no sidecar for this clip.",
        }
    latest = clicks[-1] if clicks else None
    early = latest is None or latest <= EARLY_SEC or latest <= 0.2 * duration
    if early:
        pool = _candidates(mode, data, 0.0, duration + 0.5)
    else:
        pool = _candidates(mode, data, latest - 0.75, latest + 1.25)
        if not pool:
            pool = _candidates(mode, data, 0.0, duration + 0.5)
    winner = _pick_winner(pool)
    if winner is None:
        return {
            "sidecar": True,
            "label": None,
            "time": None,
            "frames": [],
            "note": "The sidecar has no person, cyclist, or close vehicle to mark.",
        }
    return {
        "sidecar": True,
        "label": winner["label"],
        "time": round(float(winner["t"]), 2),
        "frames": _track(mode, data, winner),
        "note": f"Main hazard: {winner['label']}.",
    }


def _score_reaction(mode: str, hazard: dict[str, Any], action: str) -> dict[str, Any]:
    label = hazard.get("label")
    sidecar = bool(hazard.get("sidecar"))
    if not sidecar:
        if mode == "walk":
            scores = {"wait": 5, "steer": 4, "slow": 3, "brake": 2, "continue": 0}
            sentence = "YOLO had no sidecar for this clip, so wait until the way is clear."
        else:
            scores = {"slow": 5, "brake": 4, "wait": 3, "steer": 2, "continue": 0}
            sentence = "YOLO had no sidecar for this clip, so slow down until the way is clear."
        return {
            "points": int(scores.get(action, 0)),
            "sentence": sentence,
            "hazard": None,
            "sidecar": False,
        }
    if not label or label not in RULES:
        scores = {"slow": 5, "wait": 4, "brake": 3, "steer": 2, "continue": 1}
        return {
            "points": int(scores.get(action, 0)),
            "sentence": "The sidecar has no close hazard in your path, so slow down and look again.",
            "hazard": None,
            "sidecar": True,
        }
    scores, sentence = RULES[label]
    if mode == "walk" and label == "person with a load":
        sentence = "A person is carrying a load, so wait and give them room."
    return {
        "points": int(scores.get(action, 0)),
        "sentence": sentence,
        "hazard": label,
        "sidecar": True,
    }


def _score_total(scores: list[int]) -> str:
    """Points earned this session over 5 per scored clip, such as 8/10."""
    earned = sum(int(points) for points in scores)
    return f"{earned}/{5 * len(scores)}"


def _hazard_for(row: dict[str, Any], clicks: list[float], duration: float) -> dict[str, Any]:
    duration = max(float(row.get("duration") or 0), duration, 1.5)
    key = (row["id"], tuple(round(item, 2) for item in clicks), round(duration, 1))
    with _hazard_lock:
        cached = _hazard_cache.get(key)
    if cached is not None:
        return cached
    hazard = _main_hazard(row["mode"], _detections(row["source"]), clicks, duration)
    with _hazard_lock:
        _hazard_cache[key] = hazard
    return hazard


def _clip_by_id(clip_id: str) -> dict[str, Any] | None:
    with _clip_lock:
        row = _clips.get(clip_id)
        return dict(row) if row else None


def _duration_of(payload: dict[str, Any], row: dict[str, Any]) -> float:
    try:
        client = float(payload.get("duration") or 0)
    except (TypeError, ValueError):
        client = 0.0
    return max(client, float(row.get("duration") or 0), 1.5)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Hazard perception</title>
<base href="/app/hazard/"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root {
    color-scheme: dark;
    --bg: #121614;
    --ink: #f3f1ea;
    --muted: #a8b0a6;
    --panel: #1c221e;
    --panel-2: #242b26;
    --line: #343c36;
    --accent: #e7ff6a;
    --accent-ink: #17200a;
    --good: #b6e3a8;
    --bad: #ffb4a8;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: #121614; }
  body {
    font-family: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse 70% 40% at 100% 0%, #2a3324 0%, transparent 55%),
      var(--bg);
  }
  a { color: var(--accent); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 12px 16px 48px; }
  .kicker { margin: 0; font-size: 0.75rem; letter-spacing: 0.14em; text-transform: uppercase; }
  .kicker a { color: var(--muted); text-decoration: none; }
  .kicker a:hover { color: var(--accent); }
  h1 {
    font-family: Fraunces, Georgia, serif;
    font-weight: 500;
    font-size: clamp(1.8rem, 3vw, 2.6rem);
    letter-spacing: -0.03em;
    line-height: 0.95;
    margin: 2px 0 4px;
  }
  p.lead, .hint { color: var(--muted); max-width: 42rem; line-height: 1.45; margin: 8px 0 0; }
  .hint { font-size: 0.92rem; }
  body.in-test .lead, body.in-test .hint { display: none; }
  .modes { display: grid; gap: 12px; margin: 16px 0; }
  @media (min-width: 800px) { .modes { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  button {
    font: inherit;
    font-weight: 600;
    border: none;
    cursor: pointer;
    background: var(--accent);
    color: var(--accent-ink);
    padding: 11px 16px;
    border-radius: 999px;
  }
  button:disabled { opacity: 0.45; cursor: default; }
  button.ghost {
    background: transparent;
    color: var(--ink);
    border: 1px solid var(--line);
    font-weight: 500;
    padding: 8px 12px;
  }
  button.ghost:hover { border-color: var(--accent); color: var(--accent); }
  button.mode {
    text-align: left;
    background: var(--panel);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 16px;
    font-weight: 400;
  }
  button.mode:hover { border-color: var(--accent); }
  button.mode strong {
    display: block;
    font-family: Fraunces, Georgia, serif;
    font-size: 1.25rem;
    font-weight: 500;
    margin-bottom: 6px;
  }
  button.mode span { color: var(--muted); line-height: 1.4; }
  button.pick {
    text-align: left;
    background: var(--panel-2);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 10px 12px;
    font-weight: 500;
  }
  button.pick:hover { border-color: var(--accent); }
  button.pick.on { border-color: var(--accent); background: #2c3324; }
  button.pick:disabled:not(.on) { opacity: 0.45; }
  button.pick.on:disabled { opacity: 1; cursor: default; }
  button:focus-visible, .bar:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #desk:focus { outline: none; }
  #desk.review {
    display: grid;
    grid-template-columns: minmax(0, 1.5fr) minmax(240px, 0.72fr);
    gap: 16px;
    align-items: start;
  }
  #desk.review #ask { margin-top: 0; }
  @media (max-width: 860px) {
    #desk.review { grid-template-columns: 1fr; }
    #desk.review #ask { margin-top: 14px; }
  }
  .stage-col { min-width: 0; }
  .stage {
    position: relative;
    background: #000;
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    aspect-ratio: 16/9;
  }
  video, #overlay { position: absolute; inset: 0; width: 100%; height: 100%; }
  video { object-fit: contain; background: #000; display: block; }
  #overlay { pointer-events: none; }
  #hit {
    position: absolute;
    inset: 0;
    z-index: 2;
    width: auto;
    height: auto;
    margin: 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: transparent;
    display: grid;
    place-items: center;
    cursor: pointer;
    font-weight: 600;
  }
  #hit.on { background: rgba(8, 10, 8, 0.38); color: inherit; }
  #hit span { display: none; }
  #hit.on span {
    display: inline-block;
    padding: 12px 18px;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
  }
  #count {
    position: absolute;
    inset: 0;
    z-index: 4;
    display: grid;
    place-items: center;
    font-family: Fraunces, Georgia, serif;
    font-size: clamp(4rem, 8vw, 6rem);
    font-weight: 500;
    letter-spacing: -0.04em;
    color: var(--ink);
    background: rgba(8, 10, 8, 0.55);
    pointer-events: none;
    text-shadow: 0 2px 18px rgba(0, 0, 0, 0.45);
  }
  .caption {
    margin: 10px 0 0;
    padding: 8px 12px;
    border-radius: 12px;
    background: var(--panel);
    border: 1px solid var(--line);
    color: var(--accent);
    font-weight: 600;
    font-size: 0.92rem;
  }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 12px 0 0; }
  .choices { display: grid; gap: 8px; }
  .bar {
    position: relative;
    height: 28px;
    background: #2a3328;
    margin-top: 10px;
    border-radius: 999px;
    cursor: pointer;
    touch-action: none;
    overflow: hidden;
  }
  .window { position: absolute; top: 0; bottom: 0; background: rgba(231, 255, 106, 0.42); pointer-events: none; }
  .playhead { position: absolute; top: 0; bottom: 0; width: 2px; background: #fff; pointer-events: none; }
  .mark {
    position: absolute;
    top: 4px;
    bottom: 4px;
    width: 8px;
    margin: 0;
    border-radius: 999px;
    background: var(--good);
    transform: translateX(-50%);
    pointer-events: none;
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 16px;
    margin-top: 14px;
  }
  h2 { font-family: Fraunces, Georgia, serif; font-weight: 500; font-size: 1.45rem; margin: 0 0 12px; }
  .status { font-size: 0.92rem; color: var(--muted); min-height: 1.2em; }
  .err { color: var(--bad); margin: 8px 0 0; }
  #done { position: sticky; top: 12px; z-index: 6; }
  #result {
    position: fixed;
    inset: 0;
    z-index: 30;
    display: grid;
    place-items: center;
    padding: 24px;
    background:
      radial-gradient(ellipse 80% 50% at 50% 0%, #2a3324 0%, transparent 60%),
      rgba(12, 16, 13, 0.96);
  }
  #confetti {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .result-card {
    position: relative;
    z-index: 1;
    text-align: center;
    max-width: 16em;
  }
  .result-card h2 {
    font-family: Fraunces, Georgia, serif;
    font-weight: 500;
    font-size: clamp(2.2rem, 6vw, 4.2rem);
    line-height: 1.05;
    letter-spacing: -0.03em;
    margin: 0 0 28px;
  }
  .hidden { display: none !important; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="kicker"><a href="/app/">All apps</a></p>
      <h1>Hazard perception</h1>
      <p class="lead" id="lead">Pick a mode, then mark the hazard as the clip plays. The second pass boxes one main hazard and asks what you should do.</p>
      <p class="hint" id="keys">Space marks a hazard at the current time. Drag the bar or use the arrow keys to move through the clip. 1 Slow down, 2 Brake, 3 Wait, 4 Steer away, 5 Continue. Enter opens the second pass or submits.</p>
    </header>
    <div id="modes" class="modes">
      <button class="mode" type="button" data-mode="driving"><strong>Driving</strong><span>Streets and traffic. A person, a cyclist, or a close vehicle.</span></button>
      <button class="mode" type="button" data-mode="forklift"><strong>Forklift driver</strong><span>Warehouse aisle. A person, another vehicle, or a rack the forks are approaching.</span></button>
      <button class="mode" type="button" data-mode="walk"><strong>Warehouse walk</strong><span>On foot in the warehouse. A forklift coming, or a person with a load.</span></button>
    </div>
    <p class="status" id="meta">Choose Driving, Forklift driver, or Warehouse walk.</p>
    <div class="row">
      <button id="done" class="hidden" type="button" disabled>Done</button>
    </div>
    <p id="err" class="err hidden"></p>
    <div id="desk" class="hidden" tabindex="0">
      <div class="stage-col">
        <div class="stage">
          <video id="vid" playsinline preload="auto"></video>
          <canvas id="overlay"></canvas>
          <button id="hit" class="on" type="button"><span id="hitLabel">Start the clip</span></button>
          <div id="count" class="hidden"></div>
        </div>
        <div id="banner" class="caption hidden"></div>
        <div id="timeline" class="bar hidden" role="slider" aria-label="Clip time" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0" aria-hidden="true" tabindex="-1"></div>
        <div class="row">
          <button id="hazardBtn" class="act" type="button" disabled>I see a hazard</button>
          <button id="again" class="ghost hidden" type="button">Different clip</button>
          <button id="change" class="ghost" type="button">Change mode</button>
          <span class="status" id="clicks"></span>
        </div>
      </div>
      <div id="ask" class="panel hidden">
        <h2>What should you do?</h2>
        <div id="choices" class="choices">
          <button type="button" class="pick" data-id="slow">1 Slow down</button>
          <button type="button" class="pick" data-id="brake">2 Brake</button>
          <button type="button" class="pick" data-id="wait">3 Wait</button>
          <button type="button" class="pick" data-id="steer">4 Steer away</button>
          <button type="button" class="pick" data-id="continue">5 Continue</button>
        </div>
        <p id="rating" class="status"></p>
      </div>
    </div>
  </div>
  <div id="result" class="hidden" role="dialog" aria-modal="true" aria-labelledby="resultLine">
    <canvas id="confetti" aria-hidden="true"></canvas>
    <div class="result-card">
      <h2 id="resultLine">Congratulations you scored</h2>
      <button id="resultModes" class="ghost" type="button">Change mode</button>
    </div>
  </div>
<script>
const $ = (id) => document.getElementById(id);
const vid = $("vid");
const overlay = $("overlay");
const desk = $("desk");
const ctx = overlay.getContext("2d");
const KEYS = {1: "slow", 2: "brake", 3: "wait", 4: "steer", 5: "continue"};
const NAMES = {slow: "Slow down", brake: "Brake", wait: "Wait", steer: "Steer away", continue: "Continue"};
let clipData = null;
let modeId = "";
let pass = 0;
let clicks = [];
let pending = null;
let answered = false;
let counting = false;
let advancing = false;
let playToken = 0;
let generation = 0;
let boxPack = null;
let seekGuard = false;
let loadingClip = false;
let sessionScores = [];
let seenSources = readSeen();
let confettiFrame = 0;

function readSeen() {
  try {
    const raw = JSON.parse(sessionStorage.getItem("hazard-perception-seen") || "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map((item) => String(item || "")).filter((item) => item.indexOf("s3://") === 0).slice(0, 40);
  } catch (e) {
    return [];
  }
}

function rememberSeen(source) {
  const text = String(source || "");
  if (text.indexOf("s3://") !== 0 || seenSources.indexOf(text) >= 0) return;
  seenSources.push(text);
  if (seenSources.length > 40) seenSources.splice(0, seenSources.length - 40);
  try { sessionStorage.setItem("hazard-perception-seen", JSON.stringify(seenSources)); } catch (e) {}
}

function scoreTotal(scores) {
  let earned = 0;
  for (const points of scores) earned += points;
  return earned + "/" + (5 * scores.length);
}

function syncDone() {
  const ready = sessionScores.length > 0 && $("result").classList.contains("hidden");
  const btn = $("done");
  btn.disabled = !ready;
  btn.classList.toggle("hidden", !ready);
}

function stopConfetti() {
  cancelAnimationFrame(confettiFrame);
  confettiFrame = 0;
  const canvas = $("confetti");
  const pen = canvas.getContext("2d");
  pen.clearRect(0, 0, canvas.width, canvas.height);
}

function startConfetti() {
  const canvas = $("confetti");
  const pen = canvas.getContext("2d");
  const colors = ["#e7ff6a", "#b6e3a8", "#f3f1ea", "#ffb4a8", "#ffe08a", "#ffffff"];
  function fit() {
    const width = window.innerWidth;
    const height = window.innerHeight;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }
  fit();
  const pieces = [];
  for (let i = 0; i < 160; i += 1) {
    const angle = -Math.PI / 2 + (Math.random() - 0.5) * 1.4;
    const speed = 5 + Math.random() * 7;
    pieces.push({
      x: canvas.width * (0.2 + Math.random() * 0.6),
      y: canvas.height * (0.18 + Math.random() * 0.22),
      w: 6 + Math.random() * 8,
      h: 9 + Math.random() * 10,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed - 2,
      rot: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.28,
      color: colors[i % colors.length],
      settled: false,
    });
  }
  cancelAnimationFrame(confettiFrame);
  const started = performance.now();
  function frame(now) {
    fit();
    pen.clearRect(0, 0, canvas.width, canvas.height);
    let moving = 0;
    const floor = canvas.height - 10;
    for (const piece of pieces) {
      if (!piece.settled) {
        piece.vy += 0.16;
        piece.x += piece.vx;
        piece.y += piece.vy;
        piece.rot += piece.vr;
        piece.vx *= 0.992;
        if (piece.y >= floor) {
          piece.y = floor;
          piece.vy *= -0.28;
          piece.vx *= 0.6;
          piece.vr *= 0.4;
          if (Math.abs(piece.vy) < 0.8) {
            piece.settled = true;
            piece.vy = 0;
            piece.vx = 0;
            piece.vr = 0;
          } else {
            moving += 1;
          }
        } else {
          moving += 1;
        }
        if (piece.x < -20) piece.x = canvas.width + 10;
        if (piece.x > canvas.width + 20) piece.x = -10;
      }
      pen.save();
      pen.translate(piece.x, piece.y);
      pen.rotate(piece.rot);
      pen.fillStyle = piece.color;
      pen.fillRect(-piece.w / 2, -piece.h / 2, piece.w, piece.h);
      pen.restore();
    }
    if (moving > 0 && now - started < 9000) confettiFrame = requestAnimationFrame(frame);
  }
  confettiFrame = requestAnimationFrame(frame);
}

function showDone() {
  if (!sessionScores.length) return;
  playToken += 1;
  generation += 1;
  loadingClip = false;
  counting = false;
  advancing = false;
  pass = 0;
  try { vid.pause(); } catch (e) {}
  vid.removeAttribute("src");
  try { vid.load(); } catch (e) {}
  desk.classList.add("hidden");
  $("modes").classList.add("hidden");
  document.body.classList.remove("in-test");
  $("resultLine").textContent = "Congratulations you scored " + scoreTotal(sessionScores);
  $("result").classList.remove("hidden");
  $("done").classList.add("hidden");
  startConfetti();
  $("resultModes").focus();
}

function showError(msg) {
  $("err").textContent = msg;
  $("err").classList.remove("hidden");
}

function clearError() {
  $("err").textContent = "";
  $("err").classList.add("hidden");
}

async function readJson(res) {
  const text = await res.text();
  if (!text) return {};
  try { return JSON.parse(text); }
  catch (e) { return {error: "Unexpected response from the server."}; }
}

function keysApply() {
  if (desk.classList.contains("hidden")) return false;
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return true;
  return el === desk || desk.contains(el);
}

function focusClip() {
  if (!desk.classList.contains("hidden")) desk.focus({preventScroll: true});
}

function setHit(label) {
  const hit = $("hit");
  $("hitLabel").textContent = label || "";
  hit.classList.toggle("on", !!label);
}

function renderClicks() {
  const count = clicks.length ? (clicks.length + " mark" + (clicks.length === 1 ? "" : "s")) : "No marks yet";
  const choice = pending ? (" · " + NAMES[pending] + " for the latest mark") : "";
  $("clicks").textContent = count + choice;
}

function durationOf() {
  const raw = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : ((clipData && clipData.duration) || 0);
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function drawBar() {
  const bar = $("timeline");
  const dur = Math.max(0.1, durationOf() || 5);
  const marks = clicks.map((mark) => {
    const left = Math.min(100, Math.max(0, (mark.t / dur) * 100));
    return `<i class="mark" style="left:${left}%"></i>`;
  }).join("");
  let hazard = "";
  if (pass === 2 && boxPack && Number.isFinite(boxPack.time)) {
    const start = Math.max(0, boxPack.time - 0.75);
    const end = Math.min(dur, boxPack.time + 1.25);
    const left = (start / dur) * 100;
    const width = Math.max(2, ((end - start) / dur) * 100);
    hazard = `<i class="window" style="left:${left}%;width:${Math.min(100 - left, width)}%"></i>`;
  }
  bar.innerHTML = `${hazard}${marks}<i class="playhead" id="head"></i>`;
  bar.classList.remove("hidden");
  bar.setAttribute("aria-hidden", "false");
  bar.tabIndex = 0;
  moveHead();
}

function moveHead() {
  const head = document.getElementById("head");
  const dur = durationOf();
  const bar = $("timeline");
  if (head && dur > 0) head.style.left = Math.min(100, ((vid.currentTime || 0) / dur) * 100) + "%";
  if (bar && dur > 0 && !bar.classList.contains("hidden")) {
    bar.setAttribute("aria-valuemax", String(Math.round(dur)));
    bar.setAttribute("aria-valuenow", String(Math.round(vid.currentTime || 0)));
  }
}

function seekTo(seconds) {
  const dur = durationOf();
  if (!(dur > 0)) return;
  const t = Math.min(dur, Math.max(0, seconds));
  seekGuard = true;
  clearTimeout(seekTo.timer);
  seekTo.timer = setTimeout(() => { seekGuard = false; }, 800);
  try { vid.currentTime = t; } catch (e) { seekGuard = false; }
  moveHead();
  if (pass >= 1) paint();
}

function contentRect() {
  const vw = vid.videoWidth || 16;
  const vh = vid.videoHeight || 9;
  const rw = overlay.clientWidth || vid.clientWidth || 16;
  const rh = overlay.clientHeight || vid.clientHeight || 9;
  const scale = Math.min(rw / vw, rh / vh);
  const w = vw * scale;
  const h = vh * scale;
  return {x: (rw - w) / 2, y: (rh - h) / 2, w, h, rw, rh};
}

function boxesAt(t) {
  const frames = (boxPack && boxPack.frames) || [];
  let best = null;
  for (const frame of frames) {
    if (frame.t <= t + 0.12) best = frame;
    else break;
  }
  if (!best) return [];
  const anchor = boxPack && Number.isFinite(boxPack.time) ? boxPack.time : best.t;
  const gap = frames.find((frame) => frame.t > best.t + 0.01);
  const hold = gap
    ? Math.min(0.6, Math.max(0.2, gap.t - best.t))
    : Math.max(0.45, anchor + 1.25 - best.t);
  if (t > best.t + hold) return [];
  return (best.boxes || []).slice(0, 1);
}

function paintLabel(text, x, y, rect) {
  ctx.font = "600 14px IBM Plex Sans, sans-serif";
  const pad = 5;
  const maxW = Math.max(40, rect.w - 8);
  const tw = Math.min(ctx.measureText(text).width, maxW);
  let lx = x;
  let ly = y - 24;
  if (ly < rect.y + 2) ly = Math.min(rect.y + rect.h - 22, y + 6);
  if (lx + tw + pad * 2 > rect.x + rect.w) lx = rect.x + rect.w - tw - pad * 2;
  if (lx < rect.x) lx = rect.x;
  ctx.fillStyle = "rgba(18, 22, 20, 0.9)";
  ctx.fillRect(lx, ly, tw + pad * 2, 20);
  ctx.fillStyle = "#e7ff6a";
  ctx.fillText(text, lx + pad, ly + 14, maxW);
}

function paint() {
  const rect = contentRect();
  const width = Math.max(1, Math.round(rect.rw));
  const height = Math.max(1, Math.round(rect.rh));
  if (overlay.width !== width || overlay.height !== height) {
    overlay.width = width;
    overlay.height = height;
  }
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (pass < 1) return;
  if (pass === 2) {
    const boxes = boxesAt(vid.currentTime || 0);
    ctx.lineWidth = 3;
    for (const box of boxes) {
      const x = rect.x + box.x * rect.w;
      const y = rect.y + box.y * rect.h;
      const w = box.w * rect.w;
      const h = box.h * rect.h;
      ctx.strokeStyle = "#e7ff6a";
      ctx.strokeRect(x, y, w, h);
      if (box.label) paintLabel(box.label, x, y, rect);
    }
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  clicks.forEach((mark, i) => {
    const x = mark.x == null ? rect.x + rect.w * 0.5 : rect.x + mark.x * rect.w;
    const y = mark.y == null ? rect.y + rect.h * 0.82 : rect.y + mark.y * rect.h;
    ctx.beginPath();
    ctx.arc(x, y, 10, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(182, 227, 168, 0.95)";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#17200a";
    ctx.stroke();
    ctx.fillStyle = "#17200a";
    ctx.font = "600 12px IBM Plex Sans, sans-serif";
    ctx.fillText(String(i + 1), x, y);
  });
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
}

function resetPass() {
  pass = 0;
  clicks = [];
  pending = null;
  answered = false;
  advancing = false;
  boxPack = null;
  seekGuard = false;
  renderClicks();
  desk.classList.remove("review");
  $("ask").classList.add("hidden");
  $("rating").textContent = "";
  $("banner").classList.add("hidden");
  $("banner").textContent = "";
  $("again").classList.add("hidden");
  $("timeline").classList.add("hidden");
  $("timeline").setAttribute("aria-hidden", "true");
  $("timeline").innerHTML = "";
  $("hazardBtn").classList.remove("hidden");
  $("hazardBtn").disabled = true;
  $("hazardBtn").textContent = "I see a hazard";
  document.querySelectorAll("#choices button").forEach((b) => {
    b.classList.remove("on");
    b.disabled = true;
  });
  setHit("Start the clip");
  try { vid.pause(); } catch (e) {}
  try { vid.currentTime = 0; } catch (e) {}
  paint();
}

function showModes() {
  playToken += 1;
  generation += 1;
  loadingClip = false;
  counting = false;
  advancing = false;
  pass = 0;
  modeId = "";
  clipData = null;
  document.body.classList.remove("in-test");
  desk.classList.remove("review");
  try { vid.pause(); } catch (e) {}
  vid.removeAttribute("src");
  vid.load();
  $("modes").classList.remove("hidden");
  desk.classList.add("hidden");
  $("meta").textContent = "Choose Driving, Forklift driver, or Warehouse walk.";
  clearError();
  syncDone();
}

async function loadMode(mode) {
  const token = ++playToken;
  generation += 1;
  const gen = generation;
  const another = seenSources.length > 0;
  modeId = mode;
  counting = false;
  advancing = false;
  pass = 0;
  loadingClip = true;
  clearError();
  try { vid.pause(); } catch (e) {}
  $("hazardBtn").disabled = true;
  document.querySelectorAll(".mode").forEach((btn) => { btn.disabled = true; });
  if (another) {
    vid.removeAttribute("src");
    try { vid.load(); } catch (e) {}
    $("modes").classList.add("hidden");
    desk.classList.remove("hidden");
    desk.classList.remove("review");
    document.body.classList.add("in-test");
    $("ask").classList.add("hidden");
    $("banner").classList.add("hidden");
    $("timeline").classList.add("hidden");
    $("count").classList.add("hidden");
    setHit("Different clip");
    $("meta").textContent = "Different clip. This is not the one you just watched.";
  } else {
    $("meta").textContent = "Finding a clip for this mode…";
  }
  try {
    const res = await fetch("api/clip", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode, seen: seenSources}),
    });
    const data = await readJson(res);
    if (token !== playToken || gen !== generation) return;
    if (!res.ok) throw new Error(data.error || "Could not load a clip");
    const already = (data.source && seenSources.indexOf(data.source) >= 0)
      || (data.video && seenSources.indexOf(data.video) >= 0);
    if (already) throw new Error("This mode only has one eligible clip in the archive.");
    clipData = data;
    rememberSeen(data.source);
    rememberSeen(data.video);
    $("modes").classList.add("hidden");
    desk.classList.remove("hidden");
    document.body.classList.add("in-test");
    vid.src = clipData.stream;
    resetPass();
    const place = clipData.place || "this place";
    if (another) {
      $("meta").textContent = "Different clip · " + clipData.mode_name + " · " + place + ". Click or press Space when you see a hazard.";
    } else {
      $("meta").textContent = clipData.mode_name + " · " + place + ". Click or press Space when you see a hazard.";
    }
    focusClip();
  } catch (err) {
    if (token !== playToken || gen !== generation) return;
    const message = String(err.message || err);
    if (message.indexOf("only one eligible clip") >= 0 || message.indexOf("already used in this session") >= 0) {
      showError(message);
      $("meta").textContent = message;
      setHit("");
      return;
    }
    showError(message);
    if (clipData) {
      if (clipData.stream) vid.src = clipData.stream;
      $("meta").textContent = another
        ? "Could not load a different clip. You can start this one again."
        : "Could not load another clip. You can start this one again.";
      document.body.classList.add("in-test");
      desk.classList.remove("hidden");
      resetPass();
    } else {
      document.body.classList.remove("in-test");
      $("meta").textContent = another ? "Could not load a different clip." : "The mode did not load a clip.";
    }
  } finally {
    if (token === playToken) loadingClip = false;
    document.querySelectorAll(".mode").forEach((btn) => { btn.disabled = false; });
  }
}

function playFromStart() {
  const token = playToken;
  try { vid.pause(); } catch (e) {}
  let started = false;
  const begin = () => {
    if (started || token !== playToken) return;
    if ((vid.currentTime || 0) > 0.3) return;
    started = true;
    vid.removeEventListener("seeked", onSeeked);
    seekGuard = false;
    const playing = vid.play();
    if (playing && playing.catch) {
      playing.catch(() => {
        if (token !== playToken) return;
        setHit("Play the clip");
        showError("The clip did not start. Click the picture to play it.");
      });
    }
  };
  const onSeeked = () => begin();
  if ((vid.currentTime || 0) <= 0.05) {
    begin();
    return;
  }
  seekGuard = true;
  vid.addEventListener("seeked", onSeeked);
  try { vid.currentTime = 0; }
  catch (e) {
    vid.removeEventListener("seeked", onSeeked);
    seekGuard = false;
    started = true;
    const playing = vid.play();
    if (playing && playing.catch) playing.catch(() => {});
    return;
  }
  setTimeout(() => {
    if (started || token !== playToken) return;
    if ((vid.currentTime || 0) > 0.3) {
      try { vid.currentTime = 0; } catch (err) {}
      setTimeout(begin, 250);
      return;
    }
    begin();
  }, 700);
}

function startPass(which) {
  if (!clipData) return;
  pass = which;
  playToken += 1;
  $("count").classList.add("hidden");
  setHit("");
  if (which === 2) {
    desk.classList.add("review");
    const note = (boxPack && boxPack.note) || "Second pass.";
    $("banner").textContent = note;
    $("banner").classList.remove("hidden");
    $("ask").classList.remove("hidden");
    document.querySelectorAll("#choices button").forEach((b) => {
      b.disabled = answered;
      b.classList.toggle("on", b.dataset.id === pending);
    });
    $("hazardBtn").disabled = true;
    $("hazardBtn").classList.add("hidden");
    $("meta").textContent = "Second pass. " + note + " Press 1 to 5, then Enter.";
  } else {
    desk.classList.remove("review");
    $("banner").classList.add("hidden");
    $("hazardBtn").classList.remove("hidden");
    $("hazardBtn").disabled = false;
    $("meta").textContent = clipData.mode_name + ". Space marks a hazard. Drag the bar to scrub. Enter opens the second pass.";
    fetch("api/prime?id=" + encodeURIComponent(clipData.id)).catch(() => {});
  }
  drawBar();
  paint();
  playFromStart();
  focusClip();
}

function countdownThen(which) {
  if (loadingClip || counting || !clipData) return;
  counting = true;
  const token = ++playToken;
  const gen = generation;
  vid.muted = true;
  setHit("");
  const prime = () => {
    if (token !== playToken || !counting) return;
    const attempt = vid.play();
    if (attempt && attempt.then) {
      attempt.then(() => { if (token === playToken && counting) vid.pause(); }).catch(() => {});
    }
  };
  try {
    if ((vid.currentTime || 0) > 0.05) {
      const onSeeked = () => {
        vid.removeEventListener("seeked", onSeeked);
        prime();
      };
      vid.addEventListener("seeked", onSeeked);
      vid.currentTime = 0;
    } else {
      prime();
    }
  } catch (e) { prime(); }
  let n = 3;
  const el = $("count");
  el.classList.remove("hidden");
  el.textContent = String(n);
  $("meta").textContent = "Starting in " + n + ".";
  const timer = setInterval(() => {
    if (token !== playToken || gen !== generation) {
      clearInterval(timer);
      el.classList.add("hidden");
      counting = false;
      return;
    }
    n -= 1;
    if (n <= 0) {
      clearInterval(timer);
      el.classList.add("hidden");
      counting = false;
      startPass(which);
      return;
    }
    el.textContent = String(n);
    $("meta").textContent = "Starting in " + n + ".";
  }, 1000);
}

function noteClick(ev) {
  if (counting || advancing || pass !== 1 || clicks.length >= 12 || !Number.isFinite(vid.currentTime)) return;
  let x = null;
  let y = null;
  if (ev && ev.clientX != null) {
    const base = overlay.getBoundingClientRect();
    const rect = contentRect();
    const px = ev.clientX - base.left;
    const py = ev.clientY - base.top;
    x = (px - rect.x) / rect.w;
    y = (py - rect.y) / rect.h;
    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) {
      x = null;
      y = null;
    }
  }
  clicks.push({t: Math.round(vid.currentTime * 100) / 100, x, y});
  renderClicks();
  drawBar();
  paint();
}

function selectAction(id) {
  if (!clipData || pass < 1 || answered || !NAMES[id]) return;
  pending = id;
  document.querySelectorAll("#choices button").forEach((b) => {
    b.classList.toggle("on", b.dataset.id === id);
  });
  renderClicks();
}

function submitSelected() {
  if (pass < 2 || answered) return;
  if (!pending) {
    $("rating").textContent = "Press 1 to 5 for the latest mark, then Enter.";
    return;
  }
  const btn = document.querySelector('#choices button[data-id="' + pending + '"]');
  choose(pending, btn);
}

function resumeIfNeeded() {
  if (!$("hit").classList.contains("on") || !vid.paused) return false;
  const playing = vid.play();
  if (playing && playing.then) playing.then(() => setHit("")).catch(() => {});
  return true;
}

$("hit").onclick = (ev) => {
  if (loadingClip) return;
  focusClip();
  if (counting) return;
  if (pass === 0) countdownThen(1);
  else if (resumeIfNeeded()) return;
  else noteClick(ev);
};
$("hazardBtn").onclick = () => {
  if (loadingClip || pass !== 1 || counting) return;
  noteClick(null);
  focusClip();
};
document.querySelectorAll(".mode").forEach((btn) => {
  btn.onclick = () => loadMode(btn.dataset.mode);
});
document.querySelectorAll("#choices button").forEach((btn) => {
  btn.onclick = () => {
    if (pass < 2 || answered) return;
    selectAction(btn.dataset.id);
    submitSelected();
  };
});

document.addEventListener("keydown", (ev) => {
  if (loadingClip || !keysApply()) return;
  const key = ev.key;
  const nudge = key === "ArrowLeft" ? -1 : key === "ArrowRight" ? 1 : 0;
  if (nudge) {
    if (pass < 1 || counting) return;
    ev.preventDefault();
    ev.stopPropagation();
    seekTo((vid.currentTime || 0) + nudge);
    return;
  }
  const marking = key === " " || key === "Spacebar";
  const action = KEYS[key];
  const enter = key === "Enter";
  if (!marking && !action && !enter) return;
  const button = ev.target && ev.target.closest && ev.target.closest("button");
  const markTarget = !!(ev.target && (ev.target.id === "hit" || ev.target.id === "hazardBtn" || (ev.target.closest && ev.target.closest("#hit"))));
  if (button && !markTarget && (marking || enter)) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (counting) return;
  if (marking) {
    if (pass === 0) countdownThen(1);
    else if (pass === 1 && resumeIfNeeded()) return;
    else if (pass === 1) noteClick(null);
    return;
  }
  if (action) {
    selectAction(action);
    return;
  }
  if (pass === 1) beginSecond();
  else if (pass === 2) submitSelected();
}, true);

vid.ontimeupdate = () => {
  moveHead();
  if (pass >= 1) paint();
};

vid.addEventListener("error", () => {
  if (!clipData || desk.classList.contains("hidden")) return;
  if (!(vid.currentSrc || vid.src)) return;
  counting = false;
  pass = 0;
  $("count").classList.add("hidden");
  setHit("Try again");
  $("again").classList.remove("hidden");
  $("meta").textContent = "The clip failed to load.";
  showError("This clip did not load. Try another clip or change mode.");
});

async function beginSecond() {
  if (loadingClip || pass !== 1 || advancing || !clipData) return;
  advancing = true;
  const token = playToken;
  const gen = generation;
  $("hazardBtn").disabled = true;
  $("meta").textContent = "Loading the main hazard…";
  try {
    const res = await fetch("api/hazard", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        id: clipData.id,
        clicks,
        duration: durationOf() || clipData.duration,
      }),
    });
    const data = await readJson(res);
    if (token !== playToken || gen !== generation || pass !== 1) {
      advancing = false;
      return;
    }
    if (!res.ok) throw new Error(data.error || data.note || "Could not read detections");
    if (Array.isArray(data.frames)) {
      data.frames = data.frames.slice().sort((a, b) => (a.t || 0) - (b.t || 0));
    }
    boxPack = data;
  } catch (err) {
    if (token !== playToken || gen !== generation || pass !== 1) {
      advancing = false;
      return;
    }
    boxPack = {
      sidecar: false,
      frames: [],
      label: null,
      time: null,
      note: "The detection sidecar could not be read.",
    };
    showError(String(err.message || err));
  }
  if (token !== playToken || gen !== generation || pass !== 1) {
    advancing = false;
    return;
  }
  countdownThen(2);
}

vid.onended = () => {
  if (loadingClip) return;
  if (seekGuard) {
    seekGuard = false;
    clearTimeout(seekTo.timer);
    return;
  }
  if (pass === 1 && !counting && !advancing) beginSecond();
};

(function bindScrub() {
  const bar = $("timeline");
  let dragging = false;
  function fraction(ev) {
    const bounds = bar.getBoundingClientRect();
    if (!(bounds.width > 0)) return 0;
    return Math.min(1, Math.max(0, (ev.clientX - bounds.left) / bounds.width));
  }
  bar.addEventListener("pointerdown", (ev) => {
    if (pass < 1 || counting) return;
    dragging = true;
    try { bar.setPointerCapture(ev.pointerId); } catch (e) {}
    seekTo(fraction(ev) * durationOf());
    ev.preventDefault();
  });
  bar.addEventListener("pointermove", (ev) => {
    if (!dragging) return;
    seekTo(fraction(ev) * durationOf());
  });
  function endDrag() { dragging = false; }
  bar.addEventListener("pointerup", endDrag);
  bar.addEventListener("pointercancel", endDrag);
})();

async function choose(id, btn) {
  if (pass < 2 || answered || !clipData) return;
  answered = true;
  document.querySelectorAll("#choices button").forEach((b) => {
    b.classList.toggle("on", b === btn || b.dataset.id === id);
  });
  $("rating").textContent = "Scoring…";
  try {
    const res = await fetch("api/review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        id: clipData.id,
        clicks,
        action: id,
        duration: durationOf() || clipData.duration,
      }),
    });
    const data = await readJson(res);
    if (!res.ok) throw new Error(data.error || "Score failed");
    const points = Number.isFinite(data.points) ? data.points : 0;
    const line = "Score " + points + " out of 5. " + (data.sentence || "");
    $("rating").textContent = line;
    $("meta").textContent = line;
    sessionScores.push(points);
    document.querySelectorAll("#choices button").forEach((b) => { b.disabled = true; });
    $("again").classList.remove("hidden");
    syncDone();
  } catch (err) {
    answered = false;
    $("rating").textContent = String(err.message || err);
    showError(String(err.message || err));
  }
}

$("again").onclick = () => { if (modeId) loadMode(modeId); };
$("change").onclick = () => showModes();
$("done").onclick = () => showDone();
$("resultModes").onclick = () => {
  stopConfetti();
  $("result").classList.add("hidden");
  showModes();
};
window.addEventListener("resize", () => { if (pass >= 1) paint(); });
</script>
</body>
</html>

"""


@app.get("/health")
def health():
    return jsonify({"status": "ok", "app": "hazard-perception"})


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/api/clip", methods=["GET", "POST"])
def api_clip():
    payload = request.get_json(silent=True) if request.method == "POST" else {}
    payload = payload or {}
    mode = str(payload.get("mode") or request.args.get("mode") or "").strip().lower()
    if mode not in MODES:
        return jsonify({"error": "Choose driving, forklift, or walk", "modes": [item["name"] for item in MODES.values()]}), 400
    try:
        row = _load_clip(mode, _seen_sources(payload.get("seen")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify(_public_clip(row))


@app.get("/api/prime")
def api_prime():
    row = _clip_by_id(request.args.get("id", ""))
    if row is None:
        return jsonify({"ok": False, "error": "unknown clip"}), 404
    try:
        _detections(row["source"])
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True})


@app.post("/api/hazard")
def api_hazard():
    payload = request.get_json(force=True, silent=True) or {}
    row = _clip_by_id(str(payload.get("id") or ""))
    if row is None:
        return jsonify({"sidecar": False, "frames": [], "note": "YOLO had no sidecar for this clip.", "error": "unknown clip"}), 404
    duration = _duration_of(payload, row)
    try:
        hazard = _hazard_for(row, _click_times(payload, duration), duration)
    except Exception as exc:
        return jsonify({"sidecar": False, "frames": [], "label": None, "time": None, "note": str(exc), "error": str(exc)}), 503
    return jsonify(hazard)


@app.post("/api/review")
def api_review():
    payload = request.get_json(force=True, silent=True) or {}
    row = _clip_by_id(str(payload.get("id") or ""))
    if row is None:
        return jsonify({"error": "unknown clip"}), 404
    duration = _duration_of(payload, row)
    action = str(payload.get("action") or "")
    if action not in ACTION_LABELS:
        return jsonify({"error": "Choose slow, brake, wait, steer, or continue"}), 400
    try:
        hazard = _hazard_for(row, _click_times(payload, duration), duration)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    scored = _score_reaction(row["mode"], hazard, action)
    return jsonify(scored)


def _media_type(header: str | None) -> str:
    """Browsers reject a clip whose type is binary/octet-stream."""
    raw = (header or "").split(";", 1)[0].strip().lower()
    if raw.startswith("video/"):
        return raw
    return "video/mp4"


@app.get("/api/stream")
def stream_clip():
    source = request.args.get("source", "")
    if not source.startswith("s3://"):
        return jsonify({"error": "invalid source"}), 400
    try:
        token = _login()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    upstream_headers = {"Accept-Encoding": "identity"}
    if request.headers.get("Range"):
        upstream_headers["Range"] = request.headers["Range"]
    upstream = requests.get(
        f"{VSS_URL}/api/v1/videos/stream",
        params={"source": source, "token": token},
        stream=True,
        timeout=120,
        headers=upstream_headers,
    )
    if upstream.status_code >= 400:
        body = upstream.content
        upstream.close()
        return Response(body, status=upstream.status_code)
    headers = {
        "Content-Type": _media_type(upstream.headers.get("Content-Type")),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        headers["Content-Range"] = upstream.headers["Content-Range"]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(generate()), status=upstream.status_code, headers=headers)


def _self_check() -> None:
    shape = (1080, 1920)

    def det(label: str, conf: float, x: float, y: float, w: float, h: float) -> dict[str, Any]:
        height, width = shape
        return {
            "label": label,
            "confidence": conf,
            "bbox": [x * width, y * height, (x + w) * width, (y + h) * height],
        }

    def frames_of(*rows: tuple[float, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "video_shape": list(shape),
            "frames": [
                {"time_sec": moment, "shape": list(shape), "detections": dets}
                for moment, dets in rows
            ],
        }

    person = det("person", 0.9, 0.42, 0.42, 0.14, 0.36)
    bike = det("bicycle", 0.88, 0.40, 0.50, 0.16, 0.18)
    close_car = det("car", 0.9, 0.40, 0.48, 0.22, 0.22)
    distant = det("car", 0.92, 0.70, 0.20, 0.05, 0.05)
    light = det("traffic light", 0.95, 0.10, 0.05, 0.08, 0.16)
    bench = det("bench", 0.95, 0.30, 0.60, 0.30, 0.20)
    clock = det("clock", 0.99, 0.45, 0.45, 0.20, 0.30)
    truck = det("truck", 0.9, 0.35, 0.40, 0.28, 0.30)
    fridge = det("refrigerator", 0.8, 0.40, 0.45, 0.18, 0.22)

    mixed = frames_of(
        (2.0, [distant, light, bench, clock, close_car, bike, person]),
    )
    picked = _main_hazard("driving", mixed, [2.0], 5)
    assert picked["label"] == "person stepping out", picked
    assert picked["sidecar"] is True
    assert all(len(frame["boxes"]) == 1 for frame in picked["frames"])
    assert picked["frames"][0]["boxes"][0]["label"] == "person stepping out"

    cyclist = _main_hazard(
        "driving",
        frames_of((3.0, [bike, close_car, distant, light])),
        [3.0],
        5,
    )
    assert cyclist["label"] == "cyclist", cyclist

    nothing = _main_hazard(
        "driving",
        frames_of((1.5, [distant, light, bench, clock])),
        [1.5],
        5,
    )
    assert nothing["sidecar"] is True and nothing["label"] is None, nothing

    missing = _main_hazard("driving", None, [1.0], 5)
    assert missing["sidecar"] is False
    assert "no sidecar" in missing["note"]

    early = _main_hazard(
        "driving",
        frames_of((0.2, [clock]), (3.6, [person])),
        [0.2],
        5,
    )
    assert early["label"] == "person stepping out", early

    local = _main_hazard(
        "driving",
        frames_of((0.4, [person]), (3.0, [bike])),
        [3.0],
        5,
    )
    assert local["label"] == "cyclist", local

    walk_person = _main_hazard("walk", frames_of((2.0, [person, truck])), [2.0], 5)
    assert walk_person["label"] == "person with a load", walk_person

    walk_truck = _main_hazard("walk", frames_of((2.0, [truck, distant, bench])), [2.0], 5)
    assert walk_truck["label"] == "forklift", walk_truck

    aisle = _main_hazard("forklift", frames_of((2.0, [person, fridge, bench, clock])), [2.0], 5)
    assert aisle["label"] == "person in the aisle", aisle

    rack = _main_hazard("forklift", frames_of((2.2, [fridge, bench, light])), [2.2], 5)
    assert rack["label"] == "rack ahead", rack

    scored = _score_reaction("driving", picked, "brake")
    assert scored["points"] == 5 and scored["sentence"].endswith(".")
    assert _score_reaction("driving", picked, "continue")["points"] == 0
    bare = _score_reaction("driving", missing, "slow")
    assert bare["points"] == 5 and "no sidecar" in bare["sentence"]

    for spec in MODES.values():
        assert "\n" not in spec["query"]
        for filters in spec["filters"]:
            body = _search_body(spec["query"], filters)
            assert body["llm_top_n"] >= 1
            assert body["metadata_filters"] == filters
    assert MODES["driving"]["filters"][0] == {"capture_type": "streets"}
    assert WAREHOUSE_FILTER in MODES["forklift"]["filters"]
    assert WAREHOUSE_FILTER in MODES["walk"]["filters"]
    assert _allows("driving", {"location": "warehouse3", "camera_id": "sdg_warehouse_cam-2", "capture_type": "warehouse"}) is False
    assert _allows("forklift", {"location": "new_york", "camera_id": "nyc_streets_cam-1", "capture_type": "streets"}) is False
    assert "Driving" in PAGE and "Forklift" in PAGE and "Space" in PAGE
    assert "function seekTo" in PAGE and "bindScrub" in PAGE and "seekGuard" in PAGE
    assert "if (pass === 2)" in PAGE and "if (pass < 1) return" in PAGE
    assert "Drag the bar" in PAGE
    assert "Different clip" in PAGE and "seen" in PAGE
    assert "only one eligible clip" in PAGE
    assert "Congratulations you scored" in PAGE
    assert "seekGuard && !atEnd" not in PAGE
    assert 'id="done"' in PAGE
    assert "function startConfetti" in PAGE
    assert _score_total([5, 3]) == "8/10"
    assert _media_type("binary/octet-stream") == "video/mp4"
    assert _media_type("video/mp4") == "video/mp4"
    assert _media_type("video/webm; charset=binary") == "video/webm"
    assert _media_type(None) == "video/mp4"
    sample = {
        "chunk_results": [
            {
                "preview_source": "s3://bucket/one.mp4",
                "location": "new_york",
                "camera_id": "nyc_streets_cam-1",
                "capture_type": "streets",
                "similarity_score": 0.9,
            },
            {
                "preview_source": "s3://bucket/two.mp4",
                "location": "new_york",
                "camera_id": "nyc_streets_cam-1",
                "capture_type": "streets",
                "similarity_score": 0.4,
            },
        ]
    }
    kept = _best_chunk("driving", sample, {"s3://bucket/one.mp4"})
    assert kept is not None and kept[0]["preview_source"].endswith("two.mp4")
    assert _best_chunk("driving", sample, {"s3://bucket/one.mp4", "s3://bucket/two.mp4"}) is None
    assert _seen_sources(["s3://bucket/one.mp4", "nope", "s3://bucket/two.mp4"]) == {
        "s3://bucket/one.mp4",
        "s3://bucket/two.mp4",
    }
    assert _video_identity("s3://bucket/ride_chunk_0000.mp4") == _video_identity(
        "s3://bucket/ride_chunk_0004.mp4", "s3://bucket/ride.mp4"
    )
    parent = {
        "chunk_results": [
            {
                "preview_source": "s3://bucket/ride_chunk_0000.mp4",
                "original_video": "s3://bucket/ride.mp4",
                "location": "new_york",
                "camera_id": "nyc_streets_cam-1",
                "capture_type": "streets",
                "similarity_score": 0.95,
            },
            {
                "preview_source": "s3://bucket/ride_chunk_0004.mp4",
                "original_video": "s3://bucket/ride.mp4",
                "location": "new_york",
                "camera_id": "nyc_streets_cam-1",
                "capture_type": "streets",
                "similarity_score": 0.9,
            },
            {
                "preview_source": "s3://bucket/other_street.mp4",
                "original_video": "s3://bucket/other_street.mp4",
                "location": "new_york",
                "camera_id": "nyc_streets_cam-1",
                "capture_type": "streets",
                "similarity_score": 0.3,
            },
        ]
    }
    other = _best_chunk("driving", parent, {"s3://bucket/ride_chunk_0000.mp4"})
    assert other is not None and other[0]["preview_source"].endswith("other_street.mp4"), other
    assert _search_body("person crossing", {"capture_type": "streets"}, 80)["top_k"] == 80


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
