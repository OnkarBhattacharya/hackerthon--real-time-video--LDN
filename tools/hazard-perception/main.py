#!/usr/bin/env python3
"""Hazard-perception test. Proxied by hackathon-app at /hazard.

One main hazard is boxed on the second pass. The clip search follows the
mode the tester picked, and the first pass does not wait on detections.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

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
_last_source: dict[str, str] = {}
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


def _search_body(query: str, filters: dict[str, str]) -> dict[str, Any]:
    if "\n" in query or not query.strip():
        raise ValueError("search query must be one line")
    return {
        "query": query,
        "top_k": 8,
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


def _best_chunk(
    mode: str, payload: dict[str, Any], avoid: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows = {
        row.get("source"): row
        for row in (payload.get("results") or [])
        if isinstance(row, dict)
    }
    best: tuple[dict[str, Any], dict[str, Any]] | None = None
    best_score = -1e9
    for chunk in payload.get("chunk_results") or []:
        if not isinstance(chunk, dict):
            continue
        source = str(chunk.get("preview_source") or "")
        if not source.startswith("s3://"):
            continue
        if not _allows(mode, chunk):
            continue
        row = rows.get(source) or {}
        score = _rank(mode, chunk, row)
        if source and source == avoid:
            score -= 0.2
        if score > best_score:
            best_score = score
            best = (chunk, row)
    return best


def _search(query: str, filters: dict[str, str]) -> dict[str, Any]:
    response = _vss("POST", "/api/v1/search", json=_search_body(query, filters), timeout=50)
    if response.status_code >= 400:
        raise RuntimeError(f"search HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("search returned an unexpected payload")
    return payload


def _load_clip(mode: str) -> dict[str, Any]:
    """Search only. Detections wait until the second pass."""
    spec = MODES[mode]
    avoid = _last_source.get(mode, "")
    found: tuple[dict[str, Any], dict[str, Any]] | None = None
    used_filters: dict[str, str] | None = None
    for filters in spec["filters"]:
        found = _best_chunk(mode, _search(spec["query"], filters), avoid)
        if found:
            used_filters = filters
            break
    if not found or used_filters is None:
        if mode == "driving":
            raise RuntimeError("No street or traffic clip is indexed for this mode")
        raise RuntimeError("No warehouse3 clip is indexed for this mode")
    chunk, row = found
    source = str(chunk.get("preview_source") or "")
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
            "duration": round(max(1.5, duration), 2),
            "stream": "api/stream?source=" + quote(source, safe=""),
        }
        _clips[clip_id] = stored
        _last_source[mode] = source
        return stored


def _public_clip(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "mode": row["mode"],
        "mode_name": row["mode_name"],
        "place": row["place"],
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
<style>
  :root { color-scheme: dark; --bg:#14181c; --ink:#f4f1ea; --muted:#b7c0c7; --line:#2c3842; --accent:#f0b429; --panel:#1c242c; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background:var(--bg); color:var(--ink); }
  .wrap { max-width: 880px; margin: 0 auto; padding: 28px 18px 64px; }
  a { color: var(--accent); }
  h1 { font-family: Georgia, serif; font-weight: 500; font-size: clamp(2rem, 4vw, 2.8rem); margin: 8px 0; }
  p.lead, .hint { color: var(--muted); max-width: 42rem; line-height: 1.45; }
  .modes { display: grid; gap: 10px; margin: 16px 0; }
  button.mode { text-align: left; background: var(--panel); color: var(--ink); border: 1px solid var(--line); padding: 14px; cursor: pointer; }
  button.mode strong { display: block; font-size: 1.05rem; margin-bottom: 4px; }
  button.mode span { color: var(--muted); }
  button.mode:disabled { opacity: 0.55; cursor: default; }
  #desk:focus { outline: 1px solid var(--accent); outline-offset: 4px; }
  .stage { position: relative; background: #000; border: 1px solid var(--line); aspect-ratio: 16/9; }
  video, #overlay { position:absolute; inset:0; width:100%; height:100%; }
  video { object-fit: contain; background:#000; }
  #overlay { pointer-events: none; }
  #hit { position:absolute; inset:0; z-index:2; border:0; background:transparent; color:transparent; cursor:pointer; }
  #hit.on { color:#fff; background:rgba(0,0,0,.35); font-size:1.05rem; }
  #banner, #count { z-index:3; }
  #banner { position:absolute; left:12px; right:12px; bottom:12px; background:rgba(20,16,8,.88); color:var(--accent); padding:8px 10px; font-weight:600; }
  #count { position:absolute; inset:0; display:grid; place-items:center; font-size:4rem; background:rgba(0,0,0,.45); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:14px 0; }
  button.act, button.go { background:var(--accent); color:#1a1404; border:0; padding:10px 14px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); padding:10px 14px; cursor:pointer; }
  button.pick { text-align:left; background:var(--panel); color:var(--ink); border:1px solid var(--line); padding:10px 12px; cursor:pointer; }
  button.pick.on { border-color: var(--accent); }
  button.pick:disabled { opacity: 0.55; cursor: default; }
  .choices { display:grid; gap:8px; }
  .bar { position:relative; height:22px; background:#2a343d; margin-top:8px; cursor:pointer; touch-action:none; }
  .window { position:absolute; top:0; bottom:0; background:rgba(240,180,41,.85); pointer-events:none; }
  .playhead { position:absolute; top:0; bottom:0; width:2px; background:#fff; pointer-events:none; }
  .mark { position:absolute; top:0; width:8px; height:22px; margin-left:-4px; background:#7dcea0; pointer-events:none; }
  .panel { background:var(--panel); border:1px solid var(--line); padding:14px; margin-top:14px; }
  .status { color: var(--muted); }
  .err { color: #ffb4a8; }
  .hidden { display:none !important; }
</style>
</head>
<body>
  <div class="wrap">
    <p><a href="/app/">All apps</a></p>
    <h1>Hazard perception</h1>
    <p class="lead">Pick a mode, then mark the hazard as the clip plays. The second pass boxes one main hazard and asks what you should do.</p>
    <p class="hint" id="keys">Space marks a hazard at the current time. Drag the bar or use the arrow keys to move through the clip. 1 Slow down, 2 Brake, 3 Wait, 4 Steer away, 5 Continue. Enter opens the second pass or submits.</p>
    <div id="modes" class="modes">
      <button class="mode" type="button" data-mode="driving"><strong>Driving</strong><span>Streets and traffic. A person, a cyclist, or a close vehicle.</span></button>
      <button class="mode" type="button" data-mode="forklift"><strong>Forklift driver</strong><span>Warehouse aisle. A person, another vehicle, or a rack the forks are approaching.</span></button>
      <button class="mode" type="button" data-mode="walk"><strong>Warehouse walk</strong><span>On foot in the warehouse. A forklift coming, or a person with a load.</span></button>
    </div>
    <p class="status" id="meta">Choose Driving, Forklift driver, or Warehouse walk.</p>
    <div id="desk" class="hidden" tabindex="0">
      <div class="stage">
        <video id="vid" playsinline preload="auto"></video>
        <canvas id="overlay"></canvas>
        <button id="hit" class="on" type="button">Start the clip</button>
        <div id="banner" class="hidden"></div>
        <div id="count" class="hidden"></div>
      </div>
      <div id="timeline" class="bar hidden" role="slider" aria-label="Clip time" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0" aria-hidden="true" tabindex="-1"></div>
      <div class="row">
        <button id="hazardBtn" class="act" type="button" disabled>I see a hazard</button>
        <button id="again" class="ghost hidden" type="button">Another clip</button>
        <button id="change" class="ghost" type="button">Change mode</button>
        <span class="status" id="clicks"></span>
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
    <p id="err" class="err hidden"></p>
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
let boxPack = null;
let seekGuard = false;

function showError(msg) {
  $("err").textContent = msg;
  $("err").classList.remove("hidden");
}

function clearError() {
  $("err").textContent = "";
  $("err").classList.add("hidden");
}

function clipFocused() {
  const el = document.activeElement;
  return !!el && (el === desk || desk.contains(el));
}

function focusClip() {
  if (!desk.classList.contains("hidden")) desk.focus({preventScroll: true});
}

function renderClicks() {
  const count = clicks.length ? (clicks.length + " mark" + (clicks.length === 1 ? "" : "s")) : "No marks yet";
  const choice = pending ? (" · " + NAMES[pending] + " for the latest mark") : "";
  $("clicks").textContent = count + choice;
}

function durationOf() {
  const raw = vid.duration || (clipData && clipData.duration) || 0;
  return Number.isFinite(raw) ? raw : 0;
}

function drawBar() {
  const bar = $("timeline");
  const dur = Math.max(0.1, durationOf() || 5);
  const marks = clicks.map((mark) => `<i class="mark" style="left:${Math.min(100, (mark.t / dur) * 100)}%"></i>`).join("");
  let hazard = "";
  if (pass === 2 && boxPack && Number.isFinite(boxPack.time)) {
    const left = Math.min(98, Math.max(0, (boxPack.time / dur) * 100));
    hazard = `<i class="window" style="left:${left}%;width:2%"></i>`;
  }
  bar.innerHTML = `${hazard}<i class="playhead" id="head"></i>${marks}`;
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
  seekTo.timer = setTimeout(() => { seekGuard = false; }, 250);
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
  return (best.boxes || []).slice(0, 1);
}

function paint() {
  const rect = contentRect();
  if (overlay.width !== Math.round(rect.rw) || overlay.height !== Math.round(rect.rh)) {
    overlay.width = Math.max(1, Math.round(rect.rw));
    overlay.height = Math.max(1, Math.round(rect.rh));
  }
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (pass < 1) return;
  if (pass === 2) {
  const boxes = boxesAt(vid.currentTime || 0);
  ctx.lineWidth = 3;
  ctx.font = "600 16px sans-serif";
  for (const box of boxes) {
    const x = rect.x + box.x * rect.w;
    const y = rect.y + box.y * rect.h;
    const w = box.w * rect.w;
    const h = box.h * rect.h;
    ctx.strokeStyle = "#f0b429";
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = "#f0b429";
    ctx.fillText(box.label || "", x + 4, Math.max(18, y - 6));
  }
  }
  clicks.forEach((mark, i) => {
    const x = mark.x == null ? rect.x + rect.w * 0.5 : rect.x + mark.x * rect.w;
    const y = mark.y == null ? rect.y + rect.h * 0.82 : rect.y + mark.y * rect.h;
    ctx.beginPath();
    ctx.arc(x, y, 9, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(125,206,160,0.95)";
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
    ctx.fillStyle = "#102117";
    ctx.font = "12px sans-serif";
    ctx.fillText(String(i + 1), x - 3, y + 4);
  });
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
  $("ask").classList.add("hidden");
  $("rating").textContent = "";
  $("banner").classList.add("hidden");
  $("again").classList.add("hidden");
  $("timeline").classList.add("hidden");
  $("timeline").setAttribute("aria-hidden", "true");
  $("timeline").innerHTML = "";
  $("hit").classList.add("on");
  $("hit").textContent = "Start the clip";
  $("hazardBtn").disabled = true;
  document.querySelectorAll("#choices button").forEach((b) => {
    b.classList.remove("on");
    b.disabled = true;
  });
  vid.pause();
  try { vid.currentTime = 0; } catch (e) {}
  paint();
}

function showModes() {
  playToken += 1;
  counting = false;
  modeId = "";
  clipData = null;
  vid.removeAttribute("src");
  vid.load();
  $("modes").classList.remove("hidden");
  $("desk").classList.add("hidden");
  $("meta").textContent = "Choose Driving, Forklift driver, or Warehouse walk.";
  clearError();
}

async function loadMode(mode) {
  const token = ++playToken;
  modeId = mode;
  counting = false;
  clearError();
  document.querySelectorAll(".mode").forEach((btn) => { btn.disabled = true; });
  $("meta").textContent = "Finding a clip for this mode…";
  try {
    const res = await fetch("api/clip", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode}),
    });
    const data = await res.json();
    if (token !== playToken) return;
    if (!res.ok) throw new Error(data.error || "Could not load a clip");
    clipData = data;
    $("modes").classList.add("hidden");
    $("desk").classList.remove("hidden");
    vid.src = clipData.stream;
    resetPass();
    $("meta").textContent = clipData.mode_name + " · " + clipData.place + ". Click or press Space when you see a hazard.";
    focusClip();
  } catch (err) {
    if (token !== playToken) return;
    $("meta").textContent = "The mode did not load a clip.";
    showError(String(err.message || err));
  } finally {
    document.querySelectorAll(".mode").forEach((btn) => { btn.disabled = false; });
  }
}

function startPass(which) {
  if (!clipData) return;
  pass = which;
  $("hit").classList.remove("on");
  $("hit").textContent = "";
  if (which === 2) {
    const note = (boxPack && boxPack.note) || "Second pass.";
    $("banner").textContent = note;
    $("banner").classList.remove("hidden");
    $("ask").classList.remove("hidden");
    document.querySelectorAll("#choices button").forEach((b) => {
      b.disabled = false;
      b.classList.toggle("on", b.dataset.id === pending);
    });
    $("hazardBtn").disabled = true;
    $("meta").textContent = "Second pass. " + note + " Press 1 to 5, then Enter.";
  } else {
    $("hazardBtn").disabled = false;
    $("meta").textContent = clipData.mode_name + ". Space marks a hazard. Drag the bar to scrub. Enter opens the second pass.";
    fetch("api/prime?id=" + encodeURIComponent(clipData.id)).catch(() => {});
  }
  seekGuard = false;
  playToken += 1;
  try { vid.currentTime = 0; } catch (e) {}
  drawBar();
  paint();
  const playing = vid.play();
  if (playing && playing.catch) playing.catch(() => {});
  focusClip();
}

function countdownThen(which) {
  if (counting || !clipData) return;
  counting = true;
  const token = ++playToken;
  vid.muted = true;
  const attempt = vid.play();
  if (attempt && attempt.then) {
    attempt.then(() => { if (token === playToken && counting) vid.pause(); }).catch(() => {});
  }
  let n = 3;
  const el = $("count");
  el.classList.remove("hidden");
  el.textContent = String(n);
  const timer = setInterval(() => {
    if (token !== playToken) {
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
  }, 700);
}

function noteClick(ev) {
  if (pass !== 1 || !Number.isFinite(vid.currentTime)) return;
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

$("hit").onclick = (ev) => {
  focusClip();
  if (pass === 0) countdownThen(1);
  else noteClick(ev);
};
$("hazardBtn").onclick = () => {
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
  if (!clipFocused()) return;
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
  ev.preventDefault();
  ev.stopPropagation();
  if (counting) return;
  if (marking) {
    if (pass === 0) countdownThen(1);
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

async function beginSecond() {
  if (pass !== 1 || advancing || !clipData) return;
  advancing = true;
  $("hazardBtn").disabled = true;
  $("meta").textContent = "Loading the main hazard…";
  const token = playToken;
  try {
    const res = await fetch("api/hazard", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        id: clipData.id,
        clicks,
        duration: vid.duration || clipData.duration,
      }),
    });
    const data = await res.json();
    if (token !== playToken) return;
    if (!res.ok) throw new Error(data.error || data.note || "Could not read detections");
    boxPack = data;
  } catch (err) {
    if (token !== playToken) return;
    boxPack = {
      sidecar: false,
      frames: [],
      label: null,
      time: null,
      note: "The detection sidecar could not be read.",
    };
    showError(String(err.message || err));
  }
  if (token !== playToken) return;
  countdownThen(2);
}

vid.onended = () => {
  if (seekGuard) {
    seekGuard = false;
    clearTimeout(seekTo.timer);
    return;
  }
  if (pass === 1) beginSecond();
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
        duration: vid.duration || clipData.duration,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Score failed");
    $("rating").textContent = "Score " + data.points + " out of 5. " + data.sentence;
    $("again").classList.remove("hidden");
  } catch (err) {
    answered = false;
    $("rating").textContent = String(err.message || err);
  }
}

$("again").onclick = () => { if (modeId) loadMode(modeId); };
$("change").onclick = () => showModes();
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
        row = _load_clip(mode)
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


@app.get("/api/stream")
def stream_clip():
    source = request.args.get("source", "")
    if not source.startswith("s3://"):
        return jsonify({"error": "invalid source"}), 400
    try:
        token = _login()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    upstream = requests.get(
        f"{VSS_URL}/api/v1/videos/stream",
        params={"source": source, "token": token},
        stream=True,
        timeout=120,
        headers={"Range": request.headers.get("Range", "")} if request.headers.get("Range") else {},
    )
    if upstream.status_code >= 400:
        body = upstream.content
        upstream.close()
        return Response(body, status=upstream.status_code)
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
