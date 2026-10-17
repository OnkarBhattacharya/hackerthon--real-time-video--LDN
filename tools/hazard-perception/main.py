#!/usr/bin/env python3
"""Hazard perception driving test. Served at / and proxied by hackathon-app at /hazard."""

from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

PORT = int(os.environ.get("PORT", "8080"))


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

SPECS = [
    {
        "id": "cyclist",
        "query": "cyclist riding ahead in the bike lane on a busy city street",
        "hazard": "Cyclist ahead in the bike lane",
        "action": "ease",
        "why": "A rider is in front of you. Ease off and be ready to stop instead of closing the gap.",
    },
    {
        "id": "crosswalk",
        "query": "people walking across a marked crosswalk in front of waiting cars",
        "hazard": "People walking in the crosswalk",
        "action": "give",
        "why": "People are already in the crossing. Slow down and give way.",
    },
]

ACTIONS = [
    {"id": "ease", "label": "Ease off and be ready to stop"},
    {"id": "give", "label": "Slow down and give way"},
    {"id": "horn", "label": "Sound the horn and keep your speed"},
    {"id": "same", "label": "Carry on at the same speed"},
]
ACTION_LABELS = {row["id"]: row["label"] for row in ACTIONS}

app = Flask(__name__)
app.url_map.strict_slashes = False

_token_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0
_clip_lock = threading.Lock()
_clip_cache: dict[str, Any] = {"at": 0.0, "clips": []}


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
    response = requests.request(method, f"{VSS_URL}{path}", headers=headers, timeout=40, **kwargs)
    if response.status_code == 401:
        token = _login(force=True)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.request(method, f"{VSS_URL}{path}", headers=headers, timeout=40, **kwargs)
    return response


def _place(value: str) -> str:
    text = (value or "").replace("_", " ").strip()
    return text.title() if text else "Archive"


def _search_spec(spec: dict[str, str]) -> dict[str, Any] | None:
    response = _vss(
        "POST",
        "/api/v1/search",
        json={
            "query": spec["query"],
            "top_k": 3,
            "llm_top_n": 1,
            "min_similarity": 0.2,
            "include_public": True,
            "time_filter": "all",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(f"search HTTP {response.status_code}")
    data = response.json()
    chunk = (data.get("chunk_results") or [None])[0]
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
    span = end - start
    if span < 1:
        span = 5.0
    window_start = 0.4
    window_end = max(window_start + 1.0, round(span - 0.2, 2))
    return {
        "id": spec["id"],
        "place": _place(str(chunk.get("location") or "")),
        "hazard": spec["hazard"],
        "why": spec["why"],
        "action": spec["action"],
        "stream": "api/stream?source=" + quote(source, safe=""),
        "window_start": window_start,
        "window_end": window_end,
        "duration": round(span, 2),
        "source": source,
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
            raise RuntimeError("No driving clips are indexed yet")
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
    }


def _score(clicks: list[float], start: float, end: float) -> tuple[int, str]:
    inside = [t for t in clicks if start - 0.05 <= t <= end + 0.15]
    if not inside:
        return 0, "Missed. No click landed in the scoring window."
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
  p.lead { color: var(--muted); max-width: 38rem; line-height: 1.45; }
  .stage { position: relative; background: #000; border: 1px solid var(--line); aspect-ratio: 16/9; }
  video { width: 100%; height: 100%; object-fit: contain; background: #000; display: block; }
  #hit { position:absolute; inset:0; border:0; background:transparent; color:transparent; cursor:pointer; }
  #hit.on { color:#fff; background:rgba(0,0,0,.35); font-size:1.05rem; }
  #banner { position:absolute; left:12px; right:12px; bottom:12px; background:rgba(20,16,8,.88); color:var(--accent); padding:8px 10px; font-weight:600; }
  #count { position:absolute; inset:0; display:grid; place-items:center; font-size:4rem; background:rgba(0,0,0,.45); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:14px 0; }
  button.act, button.go { background:var(--accent); color:#1a1404; border:0; padding:10px 14px; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); padding:10px 14px; cursor:pointer; }
  button.pick { text-align:left; background:var(--panel); color:var(--ink); border:1px solid var(--line); padding:10px 12px; cursor:pointer; }
  button.pick.on { border-color: var(--accent); }
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
    <p class="lead">Click when you see a hazard. The second pass marks the clip and asks what you should do.</p>
    <p class="status" id="meta">Loading a driving clip…</p>
    <div class="stage">
      <video id="vid" playsinline preload="auto"></video>
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
      <div id="choices" class="choices"></div>
      <p id="rating" class="status"></p>
    </div>
    <p id="err" class="err hidden"></p>
  </div>
<script>
const $ = (id) => document.getElementById(id);
const vid = $("vid");
let pack = null;
let index = 0;
let pass = 0;
let clicks = [];
let answered = false;
let counting = false;
let playToken = 0;

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
  const marks = clicks.map((t) => `<i class="mark" style="left:${Math.min(100, (t / dur) * 100)}%"></i>`).join("");
  bar.innerHTML = `<i class="window" style="left:${left}%;width:${width}%"></i><i class="playhead" id="head"></i>${marks}`;
  bar.classList.remove("hidden");
}

function arm(which) {
  const c = clip();
  pass = which;
  answered = false;
  $("ask").classList.add("hidden");
  $("rating").textContent = "";
  $("banner").classList.add("hidden");
  $("next").classList.add("hidden");
  $("choices").querySelectorAll("button").forEach((b) => b.classList.remove("on"));
  if (which === 0) {
    clicks = [];
    renderClicks();
    $("timeline").classList.add("hidden");
    $("hit").classList.add("on");
    $("hit").textContent = "Start the clip";
    $("hazardBtn").disabled = true;
    $("meta").textContent = `Clip ${index + 1} of ${pack.clips.length} · ${c.place}. Click as soon as a hazard develops.`;
    vid.pause();
    vid.currentTime = 0;
  }
}

async function load() {
  const res = await fetch("api/clip");
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Could not load a clip");
  pack = data;
  $("choices").innerHTML = pack.actions.map((a) =>
    `<button type="button" class="pick" data-id="${a.id}">${a.label}</button>`
  ).join("");
  $("choices").querySelectorAll("button").forEach((btn) => {
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
    $("banner").textContent = "Hazard: " + c.hazard;
    $("banner").classList.remove("hidden");
    $("ask").classList.remove("hidden");
    $("hazardBtn").disabled = true;
    drawBar();
    $("meta").textContent = "Second pass. The band is the scoring window. Choose what you should do.";
  } else {
    $("hazardBtn").disabled = false;
    $("meta").textContent = "Click the picture, or the button, the moment you see a hazard.";
  }
  playToken += 1;
  vid.currentTime = 0;
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

function noteClick() {
  if (pass !== 1 || !vid.duration) return;
  clicks.push(Math.round(vid.currentTime * 100) / 100);
  renderClicks();
}

$("hit").onclick = () => {
  if (pass === 0) countdownThen(1);
  else noteClick();
};
$("hazardBtn").onclick = noteClick;

vid.ontimeupdate = () => {
  const head = document.getElementById("head");
  if (!head || !vid.duration) return;
  head.style.left = Math.min(100, (vid.currentTime / vid.duration) * 100) + "%";
};

vid.onended = () => {
  if (pass === 1) {
    pass = 2;
    countdownThen(2);
  }
};

async function choose(id, btn) {
  if (pass < 2 || answered) return;
  answered = true;
  $("choices").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
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
    $("rating").textContent = "Reaction " + data.points + "/5. " + data.timing + " " + verdict + " " + data.why;
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
    clicks: list[float] = []
    for raw in payload.get("clicks") or []:
        try:
            clicks.append(float(raw))
        except (TypeError, ValueError):
            continue
    points, timing = _score(clicks, float(row["window_start"]), float(row["window_end"]))
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

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        headers["Content-Range"] = upstream.headers["Content-Range"]
    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=headers,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
