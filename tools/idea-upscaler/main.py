#!/usr/bin/env python3
"""Idea Upscaler — boards + VO → shot list → cinematic matches from VSS archive."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

PORT = int(os.environ.get("PORT", "8080"))


def _resolve_vss_url() -> str:
    """Pods cannot resolve the external Ingress DNS — use the in-cluster Service."""
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
WANDB_API_KEY = os.environ.get("WANDB_API_KEY", "")
WANDB_TEAM = os.environ.get("WANDB_TEAM", "")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "")
WANDB_BASE = "https://api.inference.wandb.ai/v1"
WANDB_MODEL = os.environ.get("WANDB_MODEL", "meta-llama/Llama-3.1-8B-Instruct")
# Public path prefix (Ingress strips /app → / inside the container).
APP_PREFIX = "/app"

app = Flask(__name__)


@app.errorhandler(Exception)
def _json_errors(err: Exception):
    """Never return HTML error pages to the SPA fetch() calls."""
    from werkzeug.exceptions import HTTPException

    if isinstance(err, HTTPException):
        return jsonify({"error": err.description, "type": type(err).__name__}), err.code or 500
    return jsonify({"error": str(err), "type": type(err).__name__}), 500

_token_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0


def _login(force: bool = False) -> str:
    global _token, _token_at
    with _token_lock:
        if (
            not force
            and _token
            and (time.time() - _token_at) < 1500
        ):
            return _token
        if not VSS_URL or not VSS_USERNAME or not VSS_PASSWORD:
            raise RuntimeError("VSS credentials not configured")
        r = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        _token = r.json()["access_token"]
        _token_at = time.time()
        return _token


def _vss(method: str, path: str, *, retry: bool = True, **kwargs: Any) -> requests.Response:
    token = _login()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    r = requests.request(method, f"{VSS_URL}{path}", headers=headers, timeout=90, **kwargs)
    if r.status_code == 401 and retry:
        _login(force=True)
        return _vss(method, path, retry=False, headers={k: v for k, v in headers.items() if k != "Authorization"}, **kwargs)
    return r


def _heuristic_shots(logline: str, beats: str, transcript: str) -> list[dict[str, Any]]:
    chunks: list[str] = []
    for block in (beats, transcript, logline):
        block = (block or "").strip()
        if not block:
            continue
        parts = re.split(r"\n+|;(?=\s)|(?<=[.!?])\s+(?=[A-Z])", block)
        for p in parts:
            p = p.strip(" -•\t")
            if len(p) >= 12:
                chunks.append(p)
    if not chunks:
        chunks = [logline.strip() or "urban street moment"]

    # Dedupe while preserving order; clamp 3–8
    seen: set[str] = set()
    uniq: list[str] = []
    for c in chunks:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    if len(uniq) < 3:
        seeds = [
            f"Wide establishing: {logline}",
            f"Medium action beat: {uniq[0] if uniq else logline}",
            f"Close emotional beat: {transcript[:120] if transcript else logline}",
        ]
        for s in seeds:
            if s.lower() not in seen:
                uniq.append(s.strip())
                seen.add(s.lower())
    uniq = uniq[:8]
    if len(uniq) < 3:
        while len(uniq) < 3:
            uniq.append(f"Continuation beat {len(uniq)+1}: {logline}")

    shots = []
    types = ["wide establishing", "tracking medium", "insert detail", "over-shoulder", "close-up", "pov", "reaction", "closing wide"]
    moods = ["dusk urban", "kinetic", "tense", "intimate", "observational", "night neon", "quiet", "resolute"]
    for i, text in enumerate(uniq):
        shots.append(
            {
                "id": i + 1,
                "title": f"Shot {i + 1}",
                "intent": text[:280],
                "shot_type": types[i % len(types)],
                "mood": moods[i % len(moods)],
                "action": text[:160],
                "dialogue_beat": (transcript.split("\n")[i].strip() if transcript else ""),
                "search_query": _film_query(types[i % len(types)], moods[i % len(moods)], text, transcript),
            }
        )
    return shots


def _film_query(shot_type: str, mood: str, action: str, dialogue: str) -> str:
    bits = [
        f"cinematic {shot_type}",
        mood,
        action[:180],
    ]
    if dialogue:
        bits.append(f"spoken beat: {dialogue[:100]}")
    # Bias toward what's in this corpus without hard filters
    bits.append("urban street traffic pedestrians vehicles city")
    return " — ".join(b for b in bits if b).strip(" —")


def _wandb_shots(logline: str, beats: str, transcript: str) -> list[dict[str, Any]] | None:
    if not WANDB_API_KEY:
        return None
    system = (
        "You are a film AD turning director intent into a shot list for archival clip retrieval. "
        "Return ONLY valid JSON: {\"shots\":[{\"title\",\"intent\",\"shot_type\",\"mood\",\"action\",\"dialogue_beat\",\"search_query\"}]}. "
        "3 to 8 shots. search_query must be film-aware natural language for semantic video search "
        "(shot size, mood, action, dialogue). Prefer urban streets, traffic, pedestrians, bikes, "
        "highway, warehouse, indoor facility — never invent fantasy worlds."
    )
    user = json.dumps(
        {
            "logline": logline,
            "storyboard_beats": beats,
            "vo_transcript": transcript,
            "corpus_hint": "real-world street/traffic/warehouse/indoor camera archive",
        }
    )
    headers = {
        "Authorization": f"Bearer {WANDB_API_KEY}",
        "Content-Type": "application/json",
    }
    if WANDB_TEAM and WANDB_PROJECT:
        headers["OpenAI-Project"] = f"{WANDB_TEAM}/{WANDB_PROJECT}"
    try:
        r = requests.post(
            f"{WANDB_BASE}/chat/completions",
            headers=headers,
            json={
                "model": WANDB_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.4,
                "max_tokens": 1200,
            },
            timeout=60,
        )
        if r.status_code >= 400:
            return None
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return None
        data = json.loads(m.group(0))
        raw = data.get("shots") or []
        shots = []
        for i, s in enumerate(raw[:8]):
            intent = (s.get("intent") or s.get("action") or logline)[:280]
            st = s.get("shot_type") or "medium"
            mood = s.get("mood") or "observational"
            action = s.get("action") or intent
            dlg = s.get("dialogue_beat") or ""
            q = s.get("search_query") or _film_query(st, mood, action, dlg)
            shots.append(
                {
                    "id": i + 1,
                    "title": s.get("title") or f"Shot {i + 1}",
                    "intent": intent,
                    "shot_type": st,
                    "mood": mood,
                    "action": action[:200],
                    "dialogue_beat": dlg[:200],
                    "search_query": q,
                }
            )
        if len(shots) < 3:
            return None
        return shots
    except Exception:
        return None


def _row_to_match(row: dict[str, Any], *, chunk: bool = False) -> dict[str, Any] | None:
    if chunk:
        source = row.get("preview_source") or ""
        filename = row.get("filename") or (source.rsplit("/", 1)[-1] if source else "")
        start = row.get("best_match_start_sec")
        end = row.get("best_match_end_sec")
    else:
        source = row.get("source") or row.get("preview_source") or ""
        filename = source.rsplit("/", 1)[-1] if source else ""
        start = (
            row.get("segment_start_sec")
            or row.get("best_match_start_sec")
            or row.get("start_time")
        )
        end = (
            row.get("segment_end_sec")
            or row.get("best_match_end_sec")
            or row.get("end_time")
        )
    if not source:
        return None
    return {
        "source": source,
        "original_video": row.get("original_video") or "",
        "filename": filename,
        "similarity_score": row.get("similarity_score"),
        "start_sec": start,
        "end_sec": end,
        "reasoning": row.get("reasoning_content") or row.get("caption") or "",
        "location": row.get("location"),
        "camera_id": row.get("camera_id"),
        "capture_type": row.get("capture_type"),
        "tags": row.get("tags") or [],
        "playback_url": f"{APP_PREFIX}/api/stream?source={quote(source, safe='')}",
    }


def _search_shot(
    shot: dict[str, Any],
    top_k: int = 5,
    *,
    tags: list[str] | None = None,
    exclude_sources: set[str] | None = None,
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {
        "query": shot["search_query"],
        "top_k": top_k,
        "llm_top_n": 1,  # API requires >= 1
        "min_similarity": 0.2,
        "include_public": True,
        "time_filter": "all",
    }
    if tags:
        body["tags"] = tags
    r = _vss("POST", "/api/v1/search", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"search HTTP {r.status_code}: {r.text[:240]}")
    data = r.json()
    exclude = exclude_sources or set()
    matches: list[dict[str, Any]] = []
    for row in data.get("results") or []:
        m = _row_to_match(row)
        if not m or m["source"] in exclude:
            continue
        matches.append(m)
        if len(matches) >= top_k:
            break
    if not matches:
        for ch in data.get("chunk_results") or []:
            m = _row_to_match(ch, chunk=True)
            if not m or m["source"] in exclude:
                continue
            matches.append(m)
            if len(matches) >= top_k:
                break
    return matches


def _load_upload_seed(tag: str) -> dict[str, Any]:
    """Pull indexed segments for an upload tag (e.g. shittyvideo) to seed upscale."""
    tag = (tag or "").strip()
    if not tag:
        raise ValueError("upload tag is required")
    body = {
        "query": f"{tag} uploaded clip scene",
        "top_k": 20,
        "llm_top_n": 1,
        "min_similarity": 0.0,
        "tags": [tag],
        "include_public": True,
        "time_filter": "all",
    }
    r = _vss("POST", "/api/v1/search", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"seed search HTTP {r.status_code}: {r.text[:240]}")
    data = r.json()
    clips: list[dict[str, Any]] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for row in data.get("results") or []:
        m = _row_to_match(row)
        if not m or m["source"] in seen:
            continue
        seen.add(m["source"])
        clips.append(m)
        if m.get("reasoning"):
            reasons.append(m["reasoning"].strip())
    if not clips:
        for ch in data.get("chunk_results") or []:
            m = _row_to_match(ch, chunk=True)
            if not m or m["source"] in seen:
                continue
            seen.add(m["source"])
            clips.append(m)
            if m.get("reasoning"):
                reasons.append(m["reasoning"].strip())
    if not clips:
        raise RuntimeError(
            f"No indexed clips with tag '{tag}'. Wait for ingest, or check the tag spelling."
        )
    parent = clips[0].get("original_video") or ""
    filename = (parent.rsplit("/", 1)[-1] if parent else clips[0].get("filename") or "")
    summary = reasons[0][:400] if reasons else f"Upscale the upload tagged {tag}"
    beats = "\n".join(reasons[:8]) if reasons else ""
    return {
        "tag": tag,
        "filename": filename,
        "original_video": parent,
        "clips": clips,
        "summary": summary,
        "beats": beats,
        "exclude_sources": list(seen),
    }


@app.get("/health")
def health():
    return jsonify({"status": "ok", "app": "idea-upscaler", "wandb": bool(WANDB_API_KEY)})


@app.get("/api/stream")
def stream_clip():
    source = request.args.get("source", "")
    if not source.startswith("s3://"):
        return jsonify({"error": "invalid source"}), 400
    try:
        token = _login()
    except Exception as e:
        return jsonify({"error": str(e)}), 503
    url = f"{VSS_URL}/api/v1/videos/stream"
    upstream = requests.get(
        url,
        params={"source": source, "token": token},
        stream=True,
        timeout=120,
        headers={"Range": request.headers.get("Range", "")} if request.headers.get("Range") else {},
    )
    if upstream.status_code >= 400:
        return Response(upstream.content, status=upstream.status_code)

    def generate():
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    if "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    if "Content-Range" in upstream.headers:
        headers["Content-Range"] = upstream.headers["Content-Range"]
    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=headers,
        direct_passthrough=True,
    )


@app.get("/api/upload-seed")
def upload_seed():
    """Preview an indexed upload by tag (default: shittyvideo)."""
    tag = (request.args.get("tag") or "shittyvideo").strip()
    try:
        seed = _load_upload_seed(tag)
        return jsonify(
            {
                "tag": seed["tag"],
                "filename": seed["filename"],
                "original_video": seed["original_video"],
                "summary": seed["summary"],
                "beats": seed["beats"],
                "clip_count": len(seed["clips"]),
                "clips": seed["clips"],
            }
        )
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 404


@app.post("/api/upscale")
def upscale():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        logline = (payload.get("logline") or "").strip()
        beats = (payload.get("beats") or "").strip()
        transcript = (payload.get("transcript") or "").strip()
        seed_from_upload = bool(payload.get("seed_from_upload"))
        upload_tag = (payload.get("upload_tag") or "shittyvideo").strip() or "shittyvideo"

        source_upload = None
        exclude: set[str] = set()
        if seed_from_upload:
            seed = _load_upload_seed(upload_tag)
            source_upload = {
                "tag": seed["tag"],
                "filename": seed["filename"],
                "original_video": seed["original_video"],
                "summary": seed["summary"],
                "clips": seed["clips"],
            }
            exclude = set(seed["exclude_sources"])
            # Seed brief from Cosmos captions on the shitty upload
            if not logline:
                logline = (
                    f"Cinematic upscale of my rough clip ({seed['filename']}): "
                    f"{seed['summary'][:280]}"
                )
            else:
                logline = (
                    f"{logline}\n\n"
                    f"[Seeded from upload tag:{upload_tag} / {seed['filename']}] "
                    f"{seed['summary'][:220]}"
                )
            if seed["beats"]:
                beats = (beats + "\n" + seed["beats"]).strip() if beats else seed["beats"]

        if not logline:
            return jsonify(
                {
                    "error": "logline / scene intent is required "
                    "(or enable Upscale my upload so we can read your clip)"
                }
            ), 400

        splitter = "wandb"
        shots = _wandb_shots(logline, beats, transcript)
        if not shots:
            splitter = "heuristic"
            shots = _heuristic_shots(logline, beats, transcript)

        # Bias queries toward cinematic archive when seeding from a rough upload
        if seed_from_upload:
            for shot in shots:
                q = shot.get("search_query") or ""
                shot["search_query"] = (
                    f"cinematic polished production value — {q} — "
                    "urban streets traffic pedestrians vehicles dramatic light"
                )

        enriched = []
        for shot in shots:
            try:
                matches = _search_shot(shot, exclude_sources=exclude)
            except Exception as e:
                matches = []
                shot = {**shot, "search_error": str(e)}
            enriched.append({**shot, "matches": matches})

        directors_cut = []
        for shot in enriched:
            best = shot["matches"][0] if shot["matches"] else None
            directors_cut.append(
                {
                    "shot_id": shot["id"],
                    "title": shot["title"],
                    "intent": shot["intent"],
                    "shot_type": shot["shot_type"],
                    "mood": shot["mood"],
                    "best": best,
                }
            )

        return jsonify(
            {
                "splitter": splitter,
                "logline": logline,
                "seed_from_upload": seed_from_upload,
                "upload_tag": upload_tag if seed_from_upload else None,
                "source_upload": source_upload,
                "vss_url_host": VSS_URL.split("://")[-1].split("/")[0],
                "shots": enriched,
                "directors_cut": directors_cut,
                "pitch": "We start from boards and voice, not a prompt. The archive finds the cinematic version.",
            }
        )
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Idea Upscaler</title>
<base href="/app/"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet"/>
<style>
  :root {
    --bg: #e8e2d6;
    --ink: #1a1f24;
    --muted: #5a6570;
    --panel: #f7f3ea;
    --line: #c9c0b0;
    --accent: #0d6e6e;
    --accent-2: #c45c26;
    --good: #1f6b3a;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; }
  body {
    font-family: "IBM Plex Sans", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(ellipse 80% 50% at 10% -10%, #dfecec 0%, transparent 55%),
      radial-gradient(ellipse 60% 40% at 100% 0%, #f0dcc8 0%, transparent 50%),
      var(--bg);
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 64px; }
  header h1 {
    font-family: Fraunces, serif;
    font-size: clamp(2rem, 4vw, 2.8rem);
    font-weight: 700;
    margin: 0 0 6px;
    letter-spacing: -0.02em;
  }
  header p { margin: 0 0 22px; color: var(--muted); max-width: 42rem; line-height: 1.45; }
  .grid { display: grid; gap: 18px; }
  @media (min-width: 900px) {
    .grid.compose { grid-template-columns: 1.05fr 1fr; align-items: start; }
    .grid.results { grid-template-columns: 260px 1fr; min-height: 420px; }
  }
  label { display: block; font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
  textarea {
    width: 100%; border: 1px solid var(--line); background: var(--panel);
    border-radius: 2px; padding: 12px 14px; font: inherit; color: var(--ink);
    resize: vertical; min-height: 88px;
  }
  textarea:focus { outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent); border-color: var(--accent); }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; align-items: center; }
  button {
    font: inherit; font-weight: 600; border: none; cursor: pointer;
    background: var(--accent); color: #f4fffe; padding: 12px 18px; border-radius: 2px;
  }
  button:disabled { opacity: 0.55; cursor: wait; }
  button.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line); }
  .status { font-size: 0.9rem; color: var(--muted); }
  .status.err { color: #8b1e1e; }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 2px; padding: 14px;
  }
  .shot-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .shot-list button {
    width: 100%; text-align: left; background: transparent; color: var(--ink);
    border: 1px solid transparent; padding: 10px 12px;
  }
  .shot-list button:hover { border-color: var(--line); background: #fff; }
  .shot-list button.active { border-color: var(--accent); background: #fff; }
  .shot-list .meta { display: block; font-size: 0.75rem; color: var(--muted); margin-top: 2px; }
  .match {
    border-top: 1px solid var(--line); padding: 14px 0; display: grid; gap: 10px;
  }
  @media (min-width: 700px) {
    .match { grid-template-columns: 280px 1fr; }
  }
  video { width: 100%; background: #111; border-radius: 2px; aspect-ratio: 16/9; }
  .score { color: var(--accent-2); font-weight: 600; font-size: 0.85rem; }
  .reason { font-size: 0.92rem; line-height: 1.45; color: var(--ink); }
  .cut ol { margin: 8px 0 0; padding-left: 1.2rem; }
  .cut li { margin-bottom: 10px; line-height: 1.4; }
  .tag { display: inline-block; font-size: 0.72rem; padding: 2px 7px; border: 1px solid var(--line); margin-right: 4px; color: var(--muted); }
  h2 { font-family: Fraunces, serif; font-size: 1.25rem; margin: 0 0 10px; }
  .hidden { display: none !important; }
  .progress { margin: 12px 0 0; padding-left: 1.2rem; color: var(--muted); font-size: 0.88rem; }
  .progress li.done { color: var(--good); }
  .progress li.now { color: var(--accent); font-weight: 600; }
  .opt {
    margin-top: 14px; padding: 12px; border: 1px dashed var(--line); background: #fff;
    display: grid; gap: 8px;
  }
  .opt label.row {
    display: flex; align-items: flex-start; gap: 10px; text-transform: none;
    font-size: 0.95rem; font-weight: 500; letter-spacing: 0; color: var(--ink); cursor: pointer;
  }
  .opt input[type=checkbox] { margin-top: 3px; width: 16px; height: 16px; }
  .opt .hint { font-size: 0.82rem; color: var(--muted); line-height: 1.4; margin: 0 0 0 26px; }
  .opt input[type=text] {
    margin-left: 26px; width: calc(100% - 26px); max-width: 280px;
    border: 1px solid var(--line); background: var(--panel); padding: 8px 10px; font: inherit;
  }
  .source-box { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px; }
  .source-box video { max-width: 320px; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Idea Upscaler</h1>
      <p>Start from boards and voice — not a text-to-video prompt. We turn intent into a shot list and pull cinematic matches from your indexed archive.</p>
    </header>

    <section class="grid compose">
      <div class="panel">
        <label for="logline">Logline / scene intent *</label>
        <textarea id="logline" placeholder="A cyclist threads dusk traffic while a voiceover remembers leaving the city…"></textarea>
        <label for="beats" style="margin-top:12px">Storyboard notes (per beat)</label>
        <textarea id="beats" placeholder="Wide city canyon at dusk&#10;Bike lane tracking, yellow jacket ahead&#10;Crosswalk pause, phone glow&#10;Night close on helmet reflection"></textarea>
        <label for="transcript" style="margin-top:12px">Director VO / dialogue (optional)</label>
        <textarea id="transcript" placeholder="I kept the camera rolling… we were already gone before we left."></textarea>
        <div class="opt">
          <label class="row" for="seed">
            <input id="seed" type="checkbox"/>
            Upscale my upload (tag)
          </label>
          <p class="hint">Reads your indexed clip (default tag <code>shittyvideo</code>) and searches the archive for a cinematic match. Mars/fantasy only works if similar footage is already indexed.</p>
          <input id="tag" type="text" value="shittyvideo" placeholder="tag"/>
        </div>
        <ol class="progress hidden" id="progress"></ol>
        <div class="actions">
          <button id="go" type="button">Upscale idea</button>
          <button id="demo" class="ghost" type="button">Load demo brief</button>
          <button id="shitty" class="ghost" type="button">Upscale shittyvideo</button>
          <span class="status" id="status"></span>
        </div>
      </div>
      <div class="panel cut" id="cut-panel">
        <h2>Director's cut</h2>
        <p class="status" id="cut-empty">Upscale an idea to assemble an ordered cut from archive matches.</p>
        <ol id="cut-list" class="hidden"></ol>
      </div>
    </section>

    <section class="grid results panel hidden" id="results" style="margin-top:18px">
      <div>
        <h2>Shots</h2>
        <ul class="shot-list" id="shots"></ul>
      </div>
      <div>
        <h2 id="shot-title">Matches</h2>
        <p class="status" id="shot-meta"></p>
        <div id="matches"></div>
      </div>
    </section>
  </div>
<script>
const $ = (id) => document.getElementById(id);
let state = null;
let active = 0;

function setProgress(steps, activeIdx) {
  const el = $("progress");
  el.classList.remove("hidden");
  el.innerHTML = steps.map((t, i) => `<li class="${i < activeIdx ? "done" : i === activeIdx ? "now" : ""}">${escapeHtml(t)}</li>`).join("");
}

async function runUpscale({ seed }) {
  const logline = $("logline").value.trim();
  const useSeed = seed || $("seed").checked;
  if (!logline && !useSeed) {
    $("status").textContent = "Logline is required, or use Upscale shittyvideo.";
    $("status").className = "status err";
    return;
  }
  const steps = useSeed
    ? ["Reading shittyvideo captions", "Splitting shots", "Searching archive", "Building director's cut"]
    : ["Splitting shots", "Searching archive", "Building director's cut"];
  $("go").disabled = true;
  $("shitty").disabled = true;
  $("status").className = "status";
  $("status").textContent = steps[0] + "…";
  setProgress(steps, 0);
  let tick = 0;
  const timer = setInterval(() => {
    tick = Math.min(tick + 1, steps.length - 1);
    setProgress(steps, tick);
    $("status").textContent = steps[tick] + "…";
  }, 1800);
  try {
    const res = await fetch("/app/api/upscale", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        logline,
        beats: $("beats").value,
        transcript: $("transcript").value,
        seed_from_upload: useSeed,
        upload_tag: ($("tag").value || "shittyvideo").trim(),
      }),
    });
    const raw = await res.text();
    let data;
    try { data = JSON.parse(raw); }
    catch (_) { throw new Error("Server returned non-JSON (HTTP " + res.status + "). Hard-refresh. " + raw.slice(0, 80)); }
    if (!res.ok) throw new Error(data.error || res.statusText);
    state = data;
    active = 0;
    setProgress(steps, steps.length);
    render();
    const hits = (data.shots || []).reduce((n, s) => n + (s.matches || []).length, 0);
    $("status").textContent = `Ready · ${data.splitter} · ${data.shots.length} shots · ${hits} matches`;
  } catch (e) {
    $("status").className = "status err";
    $("status").textContent = String(e.message || e);
  } finally {
    clearInterval(timer);
    $("go").disabled = false;
    $("shitty").disabled = false;
  }
}

$("demo").onclick = () => {
  $("seed").checked = false;
  $("logline").value = "Dusk ride through the city — freedom framed as motion, then a quiet stop that feels like goodbye.";
  $("beats").value = "Wide establishing: canyon of buildings, fading light\nTracking medium: cyclist in yellow jacket in the bike lane\nCrosswalk beat: pedestrians and green light\nClose: cyclist paused on sidewalk with phone";
  $("transcript").value = "We told ourselves it was just another night ride.\nBut the city already knew we were leaving.";
};

$("shitty").onclick = () => {
  $("seed").checked = true;
  $("tag").value = "shittyvideo";
  $("logline").value = "Edit the hand out. Make this rough toy clip feel cinematic — close on the figure, then a wider world around it.";
  $("beats").value = "Close on hands and the small white toy\nPull back to the space around the figure\nMatch the gesture with a cleaner cinematic shot";
  $("transcript").value = "It started as a mess on the table.\nThen the shot found the story.";
  runUpscale({ seed: true });
};

$("go").onclick = () => runUpscale({ seed: false });

function render() {
  const results = $("results");
  results.classList.remove("hidden");
  results.style.display = "grid";
  const shots = state.shots || [];
  $("shots").innerHTML = shots.map((s, i) => `
    <li><button type="button" data-i="${i}" class="${i===active?"active":""}">
      <strong>${escapeHtml(s.title)}</strong>
      <span class="meta">${escapeHtml(s.shot_type)} · ${escapeHtml(s.mood)} · ${(s.matches||[]).length} hits</span>
    </button></li>`).join("");
  $("shots").querySelectorAll("button").forEach(btn => {
    btn.onclick = () => { active = +btn.dataset.i; render(); };
  });
  const s = shots[active];
  $("shot-title").textContent = s ? s.title : "Matches";
  $("shot-meta").textContent = s ? `${s.shot_type} · ${s.mood} — ${s.search_query}` : "";
  $("matches").innerHTML = (s && s.matches && s.matches.length)
    ? s.matches.map(m => `
      <div class="match">
        <video controls preload="metadata" src="${m.playback_url}"></video>
        <div>
          <div class="score">${(m.similarity_score!=null)?(Number(m.similarity_score).toFixed(3)+" similarity"):""}</div>
          <div><span class="tag">${escapeHtml(m.location||"?")}</span><span class="tag">${escapeHtml(m.camera_id||"")}</span></div>
          <p class="reason">${escapeHtml(m.reasoning||"")}</p>
          <p class="status">${escapeHtml(m.filename||"")} · ${fmtTime(m.start_sec)}–${fmtTime(m.end_sec)}</p>
        </div>
      </div>`).join("")
    : `<p class="status">No matches for this shot — try broader beats or drop filters.</p>`;

  const cut = state.directors_cut || [];
  if (cut.length) {
    $("cut-empty").classList.add("hidden");
    $("cut-list").classList.remove("hidden");
    $("cut-list").innerHTML = cut.map(c => {
      const b = c.best;
      return `<li><strong>${escapeHtml(c.title)}</strong> — ${escapeHtml(c.intent)}
        ${b ? `<br/><span class="status">Best: ${escapeHtml(b.filename||b.source||"")} (${(b.similarity_score!=null)?Number(b.similarity_score).toFixed(2):"?"})</span>` : `<br/><span class="status">No archive hit</span>`}
      </li>`;
    }).join("");
  }
}

function fmtTime(v) {
  if (v==null || v==="") return "?";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  const m = Math.floor(n/60), s = Math.floor(n%60);
  return `${m}:${String(s).padStart(2,"0")}`;
}
function escapeHtml(s) {
  return String(s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
