#!/usr/bin/env python3
"""Driving hazard-perception test. Proxied by hackathon-app at /hazard."""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

PORT = int(os.environ.get("PORT", "8080"))

VULNERABLE = {"person", "bicycle", "motorcycle"}
BOX_LABELS = VULNERABLE | {"car", "bus", "truck", "traffic light"}

ACTIONS = [
    {"id": "slow", "label": "Slow down"},
    {"id": "brake", "label": "Brake"},
    {"id": "wait", "label": "Wait"},
    {"id": "steer", "label": "Steer away"},
    {"id": "continue", "label": "Continue"},
]
ACTION_LABELS = {row["id"]: row["label"] for row in ACTIONS}

SPECS = [
    {
        "id": "street",
        "query": "driving along a busy city street with cars buses and people walking near the traffic",
    },
    {
        "id": "junction",
        "query": "city intersection with pedestrians near the crosswalk and vehicles in the traffic",
    },
]


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
_clip_cache: dict[str, Any] = {"at": 0.0, "clips": []}
_det_lock = threading.Lock()
_det_cache: dict[str, dict[str, Any] | None] = {}


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
    headers = kwargs.pop("headers", {})
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
    return text.title() if text else "Street"


def _detections(source: str) -> dict[str, Any] | None:
    with _det_lock:
        if source in _det_cache:
            return _det_cache[source]
    response = _vss("GET", "/api/v1/videos/detections", params={"source": source}, timeout=60)
    data: dict[str, Any] | None
    if response.status_code == 404:
        data = None
    elif response.status_code >= 400:
        data = None
    else:
        payload = response.json()
        data = payload if isinstance(payload, dict) else None
    with _det_lock:
        _det_cache[source] = data
    return data


def _decide(data: dict[str, Any] | None, duration: float) -> dict[str, Any]:
    """Pick the response and the click window from YOLO boxes when they exist."""
    duration = max(1.5, duration)
    if not data:
        return {
            "action": "slow",
            "hazard": "Traffic ahead on the street",
            "why": "You are in moving traffic. Slow down until the hazard is clear.",
            "window_start": 0.4,
            "window_end": round(max(1.2, duration - 0.3), 2),
            "boxes": False,
        }
    frames = data.get("frames") or []
    if frames:
        fps = float(data.get("fps") or 30) or 30
        duration = max(duration, float(frames[-1].get("time_sec") or 0) + 1.0 / fps)
    center: list[float] = []
    left: list[float] = []
    right: list[float] = []
    side_h = 0.0
    for frame in frames:
        shape = frame.get("shape") or data.get("video_shape") or [1080, 1920]
        height, width = float(shape[0]), float(shape[1])
        if width <= 0 or height <= 0:
            continue
        moment = float(frame.get("time_sec") or 0)
        for det in frame.get("detections") or []:
            if det.get("confidence", 0) < 0.45 or det.get("label") not in VULNERABLE:
                continue
            x1, _y1, x2, y2 = det["bbox"]
            cx = ((float(x1) + float(x2)) / 2) / width
            box_h = (float(y2) - float(_y1)) / height
            if box_h < 0.12:
                continue
            if 0.32 <= cx <= 0.68 and box_h >= 0.22:
                center.append(moment)
            elif cx < 0.32:
                left.append(moment)
                side_h = max(side_h, box_h)
            elif cx > 0.68:
                right.append(moment)
                side_h = max(side_h, box_h)
    if center:
        start = max(0.0, min(center) - 0.2)
        end = min(duration, max(center) + 0.35)
        return {
            "action": "brake",
            "hazard": "Someone is in your path",
            "why": "A person or rider is close and ahead of you. Brake.",
            "window_start": round(start, 2),
            "window_end": round(max(start + 0.8, end), 2),
            "boxes": True,
        }
    side_times = left + right
    if side_times:
        start = max(0.0, min(side_times) - 0.15)
        end = min(duration, max(side_times) + 0.3)
        one_side = bool(left) != bool(right)
        if one_side and side_h < 0.34:
            return {
                "action": "steer",
                "hazard": "Hazard off to one side",
                "why": "The hazard is to one side and not in your path. Steer away.",
                "window_start": round(start, 2),
                "window_end": round(max(start + 0.8, end), 2),
                "boxes": True,
            }
        return {
            "action": "slow",
            "hazard": "People near the traffic",
            "why": "People are close to the moving traffic. Slow down.",
            "window_start": round(start, 2),
            "window_end": round(max(start + 0.8, end), 2),
            "boxes": True,
        }
    return {
        "action": "continue",
        "hazard": "Street traffic is clear of your path",
        "why": "Nothing vulnerable is in your path. Continue.",
        "window_start": 0.4,
        "window_end": round(max(1.2, duration - 0.3), 2),
        "boxes": bool(frames),
    }


def _thin_boxes(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data or not data.get("frames"):
        return {"available": False, "frames": []}
    frames_out: list[dict[str, Any]] = []
    for frame in data.get("frames") or []:
        if int(frame.get("frame_index") or 0) % 3:
            continue
        shape = frame.get("shape") or data.get("video_shape") or [1080, 1920]
        height, width = float(shape[0]), float(shape[1])
        if width <= 0 or height <= 0:
            continue
        ranked = sorted(
            frame.get("detections") or [],
            key=lambda det: float(det.get("confidence") or 0),
            reverse=True,
        )
        boxes = []
        for det in ranked:
            if det.get("label") not in BOX_LABELS or float(det.get("confidence") or 0) < 0.45:
                continue
            x1, y1, x2, y2 = [float(v) for v in det["bbox"]]
            boxes.append(
                {
                    "label": det["label"],
                    "x": round(x1 / width, 4),
                    "y": round(y1 / height, 4),
                    "w": round(max(0.0, x2 - x1) / width, 4),
                    "h": round(max(0.0, y2 - y1) / height, 4),
                }
            )
            if len(boxes) >= 8:
                break
        if boxes:
            frames_out.append({"t": round(float(frame.get("time_sec") or 0), 3), "boxes": boxes})
    return {"available": bool(frames_out), "frames": frames_out}


def _search_spec(spec: dict[str, str]) -> dict[str, Any] | None:
    response = _vss(
        "POST",
        "/api/v1/search",
        json={
            "query": spec["query"],
            "top_k": 4,
            "llm_top_n": 1,
            "min_similarity": 0.15,
            "include_public": True,
            "time_filter": "all",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"search HTTP {response.status_code}")
    chunk = (response.json().get("chunk_results") or [None])[0]
    if not chunk:
        return None
    source = chunk.get("preview_source") or ""
    if not source:
        return None
    try:
        start = float(chunk.get("best_match_start_sec") or 0)
        end = float(chunk.get("best_match_end_sec") or 0)
    except (TypeError, ValueError):
        start, end = 0.0, 0.0
    span = end - start if end > start else 5.0
    detections = _detections(source)
    decision = _decide(detections, span if span >= 1 else 5.0)
    return {
        "id": spec["id"],
        "place": _place(str(chunk.get("location") or "")),
        "hazard": decision["hazard"],
        "why": decision["why"],
        "action": decision["action"],
        "stream": "api/stream?source=" + quote(source, safe=""),
        "window_start": decision["window_start"],
        "window_end": decision["window_end"],
        "duration": round(max(span, decision["window_end"]), 2),
        "source": source,
        "boxes": decision["boxes"],
    }


def _clips() -> list[dict[str, Any]]:
    with _clip_lock:
        if _clip_cache["clips"] and (time.time() - _clip_cache["at"]) < 600:
            return _clip_cache["clips"]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in SPECS:
        row = _search_spec(spec)
        if not row or row["source"] in seen:
            continue
        seen.add(row["source"])
        found.append(row)
    if not found:
        raise RuntimeError("No street or traffic clips are indexed yet")
    with _clip_lock:
        _clip_cache["clips"] = found
        _clip_cache["at"] = time.time()
        return found


def _public_clip(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "place": row["place"],
        "hazard": row["hazard"],
        "stream": row["stream"],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "duration": row["duration"],
        "boxes": row["boxes"],
    }


def _score(clicks: list[float], start: float, end: float) -> tuple[int, str]:
    inside = [t for t in clicks if start - 0.05 <= t <= end + 0.15]
    if not inside:
        return 0, "Missed. No click landed while the hazard was developing."
    earliest = min(inside)
    span = max(0.4, end - start)
    frac = min(1.0, max(0.0, (earliest - start) / span))
    if frac < 0.2:
        return 5, "Early. You clicked as the hazard developed."
    if frac < 0.4:
        return 4, "Good reaction. Your click is in the early part of the window."
    if frac < 0.6:
        return 3, "In the window, a little late."
    if frac < 0.8:
        return 2, "Late reaction."
    return 1, "Late reaction. The window was almost over."


def _click_times(payload: dict[str, Any]) -> list[float]:
    times: list[float] = []
    for raw in payload.get("clicks") or []:
        if isinstance(raw, dict):
            raw = raw.get("t")
        try:
            times.append(float(raw))
        except (TypeError, ValueError):
            continue
    return times


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
  p.lead { color: var(--muted); max-width: 40rem; line-height: 1.45; }
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
  .bar { position:relative; height:14px; background:#2a343d; margin-top:8px; }
  .window { position:absolute; top:0; bottom:0; background:rgba(240,180,41,.55); }
  .playhead { position:absolute; top:-3px; bottom:-3px; width:2px; background:#fff; }
  .mark { position:absolute; top:-4px; width:8px; height:22px; margin-left:-4px; background:#7dcea0; }
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
    <p class="lead">Play a street clip. Click when you see a hazard. The second pass shows your click marks and YOLO boxes, then asks what you should do.</p>
    <p class="status" id="meta">Loading a street clip…</p>
    <div class="stage">
      <video id="vid" playsinline preload="auto"></video>
      <canvas id="overlay"></canvas>
      <button id="hit" class="on" type="button">Start the clip</button>
      <div id="banner" class="hidden"></div>
      <div id="count" class="hidden"></div>
    </div>
    <div id="timeline" class="bar hidden" aria-hidden="true"></div>
    <div class="row">
      <button id="hazardBtn" class="act" type="button" disabled>I see a hazard</button>
      <button id="next" class="ghost hidden" type="button">Next clip</button>
      <span class="status" id="clicks"></span>
    </div>
    <div id="ask" class="panel hidden">
      <h2>What should you do?</h2>
      <div id="choices" class="choices">
        <button type="button" class="pick" data-id="slow">Slow down</button>
        <button type="button" class="pick" data-id="brake">Brake</button>
        <button type="button" class="pick" data-id="wait">Wait</button>
        <button type="button" class="pick" data-id="steer">Steer away</button>
        <button type="button" class="pick" data-id="continue">Continue</button>
      </div>
      <p id="rating" class="status"></p>
    </div>
    <p id="err" class="err hidden"></p>
  </div>
<script>
const $ = (id) => document.getElementById(id);
const vid = $("vid");
const overlay = $("overlay");
const ctx = overlay.getContext("2d");
let pack = null;
let index = 0;
let pass = 0;
let clicks = [];
let answered = false;
let counting = false;
let playToken = 0;
let boxPack = null;

function clip() { return pack.clips[index]; }

function showError(msg) {
  $("err").textContent = msg;
  $("err").classList.remove("hidden");
}

function renderClicks() {
  $("clicks").textContent = clicks.length ? (clicks.length + " click" + (clicks.length === 1 ? "" : "s")) : "";
}

function drawBar() {
  const bar = $("timeline");
  const c = clip();
  const dur = Math.max(0.1, vid.duration || c.duration || 5);
  const left = (c.window_start / dur) * 100;
  const width = Math.max(2, ((c.window_end - c.window_start) / dur) * 100);
  const marks = clicks.map((mark) => `<i class="mark" style="left:${Math.min(100, (mark.t / dur) * 100)}%"></i>`).join("");
  bar.innerHTML = `<i class="window" style="left:${left}%;width:${width}%"></i><i class="playhead" id="head"></i>${marks}`;
  bar.classList.remove("hidden");
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
    if (frame.t <= t + 0.08) best = frame;
    else break;
  }
  return best ? best.boxes : [];
}

function paint() {
  const rect = contentRect();
  if (overlay.width !== Math.round(rect.rw) || overlay.height !== Math.round(rect.rh)) {
    overlay.width = Math.max(1, Math.round(rect.rw));
    overlay.height = Math.max(1, Math.round(rect.rh));
  }
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  if (pass !== 2) return;
  ctx.lineWidth = 2;
  ctx.font = "13px sans-serif";
  for (const box of boxesAt(vid.currentTime || 0)) {
    const x = rect.x + box.x * rect.w;
    const y = rect.y + box.y * rect.h;
    const w = box.w * rect.w;
    const h = box.h * rect.h;
    ctx.strokeStyle = "#f0b429";
    ctx.strokeRect(x, y, w, h);
    ctx.fillStyle = "#f0b429";
    ctx.fillText(box.label, x + 3, Math.max(14, y + 14));
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

function arm(which) {
  const c = clip();
  pass = which;
  answered = false;
  boxPack = null;
  $("ask").classList.add("hidden");
  $("rating").textContent = "";
  $("banner").classList.add("hidden");
  $("next").classList.add("hidden");
  document.querySelectorAll("#choices button").forEach((b) => {
    b.classList.remove("on");
    b.disabled = true;
  });
  if (which === 0) {
    clicks = [];
    renderClicks();
    $("timeline").classList.add("hidden");
    $("hit").classList.add("on");
    $("hit").textContent = "Start the clip";
    $("hazardBtn").disabled = true;
    $("meta").textContent = `Clip ${index + 1} of ${pack.clips.length} · ${c.place}. Click as soon as you see a hazard.`;
    vid.pause();
    try { vid.currentTime = 0; } catch (e) {}
  }
  paint();
}

async function load() {
  const res = await fetch("api/clip");
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Could not load a clip");
  pack = data;
  document.querySelectorAll("#choices button").forEach((btn) => {
    btn.onclick = () => choose(btn.dataset.id, btn);
  });
  vid.src = clip().stream;
  arm(0);
}

function startPass(which) {
  pass = which;
  const c = clip();
  $("hit").classList.remove("on");
  $("hit").textContent = "";
  if (which === 2) {
    $("banner").textContent = "Second pass · " + c.hazard;
    $("banner").classList.remove("hidden");
    $("ask").classList.remove("hidden");
    document.querySelectorAll("#choices button").forEach((b) => { b.disabled = false; });
    $("hazardBtn").disabled = true;
    drawBar();
    const boxesNote = (boxPack && boxPack.available) ? "YOLO boxes are on the picture." : "This clip has no detection sidecar, so only your click marks are shown.";
    $("meta").textContent = "Second pass. " + boxesNote + " Then choose what you should do.";
  } else {
    $("hazardBtn").disabled = false;
    $("meta").textContent = "Click the picture, or the button, the moment you see a hazard.";
  }
  playToken += 1;
  try { vid.currentTime = 0; } catch (e) {}
  const playing = vid.play();
  if (playing && playing.catch) playing.catch(() => {});
}

function countdownThen(which) {
  if (counting) return;
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
    const bounds = vid.getBoundingClientRect();
    const rect = contentRect();
    const px = ev.clientX - bounds.left;
    const py = ev.clientY - bounds.top;
    x = (px - rect.x) / rect.w;
    y = (py - rect.y) / rect.h;
    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) {
      x = null;
      y = null;
    }
  }
  clicks.push({t: Math.round(vid.currentTime * 100) / 100, x, y});
  renderClicks();
}

$("hit").onclick = (ev) => {
  if (pass === 0) countdownThen(1);
  else noteClick(ev);
};
$("hazardBtn").onclick = () => noteClick(null);

vid.ontimeupdate = () => {
  const head = document.getElementById("head");
  if (head && vid.duration) head.style.left = Math.min(100, (vid.currentTime / vid.duration) * 100) + "%";
  if (pass === 2) paint();
};

async function beginSecond() {
  pass = 2;
  $("hazardBtn").disabled = true;
  $("meta").textContent = "Loading click marks and YOLO boxes…";
  try {
    const res = await fetch("api/boxes?id=" + encodeURIComponent(clip().id));
    boxPack = await res.json();
  } catch (err) {
    boxPack = {available: false, frames: []};
  }
  countdownThen(2);
}

vid.onended = () => {
  if (pass === 1) beginSecond();
};

async function choose(id, btn) {
  if (pass < 2 || answered) return;
  answered = true;
  document.querySelectorAll("#choices button").forEach((b) => b.classList.toggle("on", b === btn));
  $("rating").textContent = "Scoring…";
  try {
    const res = await fetch("api/review", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({id: clip().id, clicks, action: id}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Score failed");
    const verdict = data.action_ok
      ? "That is the response to take."
      : ("What you should do: " + data.expected + ".");
    $("rating").textContent = "Score " + data.points + " out of 5. " + data.timing + " " + verdict + " " + data.why;
    if (index < pack.clips.length - 1) $("next").classList.remove("hidden");
  } catch (err) {
    answered = false;
    $("rating").textContent = String(err.message || err);
  }
}

$("next").onclick = () => {
  index += 1;
  vid.src = clip().stream;
  arm(0);
};

load().catch((err) => {
  $("meta").textContent = "The test page is up, but the clip did not load.";
  showError(String(err.message || err));
});
</script>
</body>
</html>
"""


@app.get("/health")
def health():
    return jsonify({"status": "ok", "app": "hazard-perception"})


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/api/clip")
def api_clip():
    try:
        rows = _clips()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"clips": [_public_clip(row) for row in rows], "actions": ACTIONS})


@app.get("/api/boxes")
def api_boxes():
    clip_id = request.args.get("id", "")
    try:
        rows = _clips()
    except Exception as exc:
        return jsonify({"available": False, "frames": [], "error": str(exc)})
    row = next((item for item in rows if item["id"] == clip_id), None)
    if row is None:
        return jsonify({"available": False, "frames": [], "error": "unknown clip"}), 404
    return jsonify(_thin_boxes(_detections(row["source"])))


@app.post("/api/review")
def api_review():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        rows = _clips()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 503
    row = next((item for item in rows if item["id"] == payload.get("id")), None)
    if row is None:
        return jsonify({"error": "unknown clip"}), 404
    points, timing = _score(_click_times(payload), float(row["window_start"]), float(row["window_end"]))
    chosen = str(payload.get("action") or "")
    return jsonify(
        {
            "points": points,
            "timing": timing,
            "action_ok": chosen == row["action"],
            "expected": ACTION_LABELS.get(row["action"], ""),
            "why": row["why"],
            "hazard": row["hazard"],
        }
    )


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


def _warm() -> None:
    try:
        _clips()
    except Exception:
        pass


threading.Thread(target=_warm, name="hazard-warm", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
