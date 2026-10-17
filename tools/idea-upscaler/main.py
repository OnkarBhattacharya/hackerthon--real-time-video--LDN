#!/usr/bin/env python3
"""Idea Upscaler — boards + VO → shot list → cinematic matches from VSS archive."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
    vo_lines = [ln.strip() for ln in (transcript or "").split("\n") if ln.strip()]
    for i, text in enumerate(uniq):
        shots.append(
            {
                "id": i + 1,
                "title": f"Shot {i + 1}",
                "intent": text[:280],
                "shot_type": types[i % len(types)],
                "mood": moods[i % len(moods)],
                "action": text[:160],
                "dialogue_beat": vo_lines[i] if i < len(vo_lines) else "",
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


# Keep the request threshold. The embedder sometimes still returns weaker rows.
MIN_SIMILARITY = 0.2


def _score_ok(match: dict[str, Any]) -> bool:
    score = match.get("similarity_score")
    if score is None or score == "":
        return True
    try:
        return float(score) >= MIN_SIMILARITY
    except (TypeError, ValueError):
        return True


def _collect_matches(
    data: dict[str, Any],
    *,
    exclude: set[str],
    top_k: int,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in data.get("results") or []:
        m = _row_to_match(row)
        if not m or m["source"] in exclude or not _score_ok(m):
            continue
        matches.append(m)
        if len(matches) >= top_k:
            return matches
    if matches:
        return matches
    for ch in data.get("chunk_results") or []:
        m = _row_to_match(ch, chunk=True)
        if not m or m["source"] in exclude or not _score_ok(m):
            continue
        matches.append(m)
        if len(matches) >= top_k:
            break
    return matches


def _search_shot(
    shot: dict[str, Any],
    top_k: int = 5,
    *,
    tags: list[str] | None = None,
    exclude_sources: set[str] | None = None,
    metadata_filters: dict[str, Any] | None = None,
    fallback: bool = True,
) -> list[dict[str, Any]]:
    query = re.sub(r"\s+", " ", (shot.get("search_query") or "")).replace("—", "-").replace("–", "-").strip()
    filters = metadata_filters if metadata_filters is not None else shot.get("metadata_filters") or None
    exclude = exclude_sources or set()

    def call(filt: dict[str, Any] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query[:400],
            "top_k": top_k,
            "llm_top_n": 1,  # API requires >= 1
            "min_similarity": MIN_SIMILARITY,
            "include_public": True,
            "time_filter": "all",
        }
        if tags:
            body["tags"] = tags
        if filt:
            body["metadata_filters"] = filt
        r = _vss("POST", "/api/v1/search", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"search HTTP {r.status_code}: {r.text[:240]}")
        return r.json()

    data = call(filters if filters else None)
    matches = _collect_matches(data, exclude=exclude, top_k=top_k)
    if not matches and filters and fallback:
        matches = _collect_matches(call(None), exclude=exclude, top_k=top_k)
    return matches


def _load_upload_seed(tag: str) -> dict[str, Any]:
    """Pull indexed segments for an upload tag (e.g. shittyvideo) to seed upscale."""
    tag = (tag or "").strip()
    if not tag:
        raise ValueError("upload tag is required")
    # Tag name is a bad visual query ("shittyvideo" matches nothing). Search the
    # caption, then fall back without the tag and filter client-side.
    queries = [
        "hands holding a small toy figure",
        "person close-up object on a table",
        "a person moving in frame",
    ]
    data: dict[str, Any] = {}
    last_err = ""
    for q in queries:
        body = {
            "query": q,
            "top_k": 20,
            "llm_top_n": 1,
            "min_similarity": 0.0,
            "tags": [tag],
            "include_public": True,
            "time_filter": "all",
        }
        r = _vss("POST", "/api/v1/search", json=body)
        if r.status_code >= 400:
            last_err = f"seed search HTTP {r.status_code}: {r.text[:240]}"
            continue
        data = r.json()
        if data.get("results") or data.get("chunk_results"):
            break
    else:
        body = {
            "query": "hands toy figure person",
            "top_k": 30,
            "llm_top_n": 1,
            "min_similarity": 0.0,
            "include_public": True,
            "time_filter": "24h",
        }
        r = _vss("POST", "/api/v1/search", json=body)
        if r.status_code >= 400:
            raise RuntimeError(last_err or f"seed search HTTP {r.status_code}: {r.text[:240]}")
        data = r.json()
        want = tag.lower()
        data["results"] = [
            row for row in (data.get("results") or [])
            if want in [str(t).lower() for t in (row.get("tags") or [])]
            or want in str(row.get("filename") or "").lower()
            or want in str(row.get("source") or "").lower()
        ]
        data["chunk_results"] = [
            row for row in (data.get("chunk_results") or [])
            if want in [str(t).lower() for t in (row.get("tags") or [])]
            or want in str(row.get("filename") or "").lower()
        ]
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


CUTS_DIR = Path("/tmp/idea-cuts")


def _download_mp4(source: str, dest: Path) -> None:
    token = _login()
    with requests.get(
        f"{VSS_URL}/api/v1/videos/stream",
        params={"source": source, "token": token},
        stream=True,
        timeout=180,
    ) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    return max(0.0, float(out.strip() or 0))


def _srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed")[-500:])


# UI values and older espeak ids both map onto Microsoft neural voices.
NEURAL_VOICES = {
    "en-US-JennyNeural": "en-us+f3",
    "en-US-GuyNeural": "en-us+m3",
    "en-GB-SoniaNeural": "en-gb+f2",
    "en-GB-RyanNeural": "en-gb+m3",
}
VOICE_ALIASES = {espeak: neural for neural, espeak in NEURAL_VOICES.items()}
DEFAULT_VOICE = "en-US-JennyNeural"

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_UA = "IdeaUpscaler/1.0 (educational video cut; VAST builders challenge)"
MAX_CC_BYTES = 40 * 1024 * 1024
MAX_CC_SECONDS = 75.0
# Search still keeps hits at 0.2. The CC toggle only replaces one shot that
# the archive does not really have: no hit, or a weak neighbor under this score.
CC_GAP_BELOW = 0.35
_CC_OK_RE = re.compile(
    r"(creative\s+commons|\bcc(?:0|[- ]by)\b|public\s+domain)",
    re.I,
)
_CC_BAD_RE = re.compile(r"(all rights reserved|non-free|fair use)", re.I)
_CC_STOP = {
    "cinematic", "the", "a", "an", "of", "and", "with", "near", "in", "on",
    "to", "for", "from", "shot", "wide", "medium", "close", "this", "that",
}


def _has_audio(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(proc.stdout.strip())


class LevelCrossingFootageError(RuntimeError):
    """Commons could not supply a real railway clip. Do not substitute a crosswalk."""


# A city crosswalk, highway, or warehouse is not a level crossing.
_NON_RAIL_CAPTURE = {"streets", "street", "highway", "indoor", "warehouse"}
_NON_RAIL_PLACE = (
    "warehouse", "forklift", "san_francisco", "crosswalk", "sdg_warehouse",
    "nyc", "new_york", "london",
)
_RAIL_BAD_RE = re.compile(
    r"forklift|warehouse|crosswalk|zebra crossing|bike lane|airport barrier|highway traffic",
    re.I,
)
_RAIL_OK_RE = re.compile(
    r"level crossing|railroad crossing|railway crossing|grade crossing|"
    r"bahnübergang|\boverweg\b|ж\.?\s*д|"
    r"\b(railroads?|railways?|locomotive|boom gate)\b",
    re.I,
)

LEVEL_CROSSING_LOGLINE = (
    "You are helping people avoid a level crossing incident. "
    "One railway crossing, from the approach to the rule."
)
LEVEL_CROSSING_BEATS = (
    "Approach the railway level crossing\n"
    "Warning lights and the barrier come down\n"
    "Someone misuses the crossing\n"
    "The train does not slow — a near miss\n"
    "The rule: wait until the lights are out and the barrier is fully raised"
)
LEVEL_CROSSING_LINES = [
    "You are helping people avoid a level crossing incident. Walk them up to the railway crossing and stop.",
    "Show them the warning lights and the barrier. Those come down because a train is coming.",
    "If they try to beat the barrier, or cross while the lights are still on, that is the misuse.",
    "The train will not slow down for them. A near miss is not a safe gap.",
    "The rule you give them is simple. Wait until the lights are out and the barrier is fully raised.",
]
MAX_RAIL_CC_SECONDS = 180.0


def _level_crossing_shots() -> list[dict[str, Any]]:
    """Five beats in the order of a level-crossing safety film. One subject only."""
    specs = [
        {
            "title": "The approach",
            "intent": "Arrive at a railway level crossing and stop before the rails.",
            "shot_type": "wide establishing",
            "mood": "watchful",
            "action": "Road or path arriving at a railway level crossing",
            "dialogue_beat": LEVEL_CROSSING_LINES[0],
            "search_query": "railway level crossing ahead of the rails, no cars on a city crosswalk",
            "commons_files": [
                "File:Railway crossing in Halton Hills - 20210307.webm",
                "File:Level crossing in Japan - Tokyo - near Ebaramachi Station - 2022 Oct 24.ogg",
                "File:Padangbesa 1.webm",
                "File:20260720 183051 Xuetian Road Level Crossing.webm",
            ],
            "commons_queries": [
                "railway crossing level crossing filetype:video",
                "level crossing road approach filetype:video",
            ],
        },
        {
            "title": "The warning",
            "intent": "Warning lights or the barrier come down because a train is coming.",
            "shot_type": "medium",
            "mood": "alert",
            "action": "Level crossing barrier lowering or warning lights at the rails",
            "dialogue_beat": LEVEL_CROSSING_LINES[1],
            "search_query": "railway level crossing barrier lowering and warning lights",
            "commons_files": [
                "File:Gillingham Level Crossing barriers going down.webm",
                "File:Malfunctioning railway crossing warning lights.ogv",
            ],
            "commons_queries": [
                "level crossing barriers going down filetype:video",
                "railway crossing warning lights filetype:video",
            ],
        },
        {
            "title": "The misuse",
            "intent": "Someone is on the railway crossing while they should be waiting.",
            "shot_type": "medium",
            "mood": "tense",
            "action": "Person or traffic on a railway level crossing as a train is due",
            "dialogue_beat": LEVEL_CROSSING_LINES[2],
            "search_query": "pedestrian on a railway level crossing while a train passes",
            "commons_files": [
                "File:Петрозаводск, ул.Новосулажгорская, ж.д. переход.webm",
                "File:Gevaarlijke overweg-520537.ogv",
                "File:Weggebruikers attentie.... onbewaakte overweg-524524.ogv",
            ],
            "commons_queries": [
                "pedestrian railway crossing train filetype:video",
                "unguarded railway crossing overweg filetype:video",
            ],
        },
        {
            "title": "The near miss",
            "intent": "The train takes the crossing and does not slow down.",
            "shot_type": "wide",
            "mood": "severe",
            "action": "Train passing through a level crossing at speed",
            "dialogue_beat": LEVEL_CROSSING_LINES[3],
            "search_query": "train passing through a railway level crossing without slowing",
            "commons_files": [
                "File:Bahnübergang Nackenheim - Güterzug.webm",
                "File:Caltrain locomotive 905 passing by San Mateo.gk.webm",
                "File:GO Train at level crossing.webm",
                "File:MaineNarrowGaugeRailroadApril2011.ogv",
            ],
            "commons_queries": [
                "train passing level crossing filetype:video",
                "locomotive railroad crossing filetype:video",
            ],
        },
        {
            "title": "The rule",
            "intent": "Wait until the lights are out and the barrier is fully raised.",
            "shot_type": "close-up",
            "mood": "resolute",
            "action": "Level crossing warning lights or a barrier that must be fully raised",
            "dialogue_beat": LEVEL_CROSSING_LINES[4],
            "search_query": "railway crossing warning lights and barrier fully raised before anyone crosses",
            "commons_files": [
                "File:Malfunctioning railway crossing warning lights.ogv",
                "File:Gevaarlijke overweg-520537.ogv",
                "File:Gillingham Level Crossing barriers going down.webm",
            ],
            "commons_queries": [
                "railway crossing warning lights filetype:video",
                "level crossing barrier lights filetype:video",
            ],
        },
    ]
    return [{"id": i + 1, **spec} for i, spec in enumerate(specs)]


def _text_is_railway(*parts: str) -> bool:
    blob = " ".join(p for p in parts if p)
    if not blob.strip():
        return False
    if _RAIL_BAD_RE.search(blob) and not _RAIL_OK_RE.search(blob):
        return False
    if _RAIL_BAD_RE.search(blob) and not re.search(
        r"level crossing|railway|railroad|overweg|bahnübergang|ж\.?\s*д", blob, re.I
    ):
        return False
    return bool(_RAIL_OK_RE.search(blob))


def _archive_is_level_crossing(match: dict[str, Any]) -> bool:
    """True only for a real railway crossing. Streets and the warehouse never qualify."""
    capture = str(match.get("capture_type") or "").strip().lower()
    location = str(match.get("location") or "").strip().lower()
    camera = str(match.get("camera_id") or "").strip().lower()
    if capture in _NON_RAIL_CAPTURE:
        return False
    place = f"{location} {camera}"
    if any(token in place for token in _NON_RAIL_PLACE):
        return False
    if any(token in place for token in ("street", "highway", "indoor")):
        return False
    return _text_is_railway(
        capture,
        location,
        camera,
        str(match.get("filename") or ""),
        str(match.get("reasoning") or ""),
        str(match.get("original_video") or ""),
    )


def _search_level_crossing_archive() -> list[dict[str, Any]]:
    """Archive first, but drop anything that is not actually a railway crossing."""
    queries = [
        "railway level crossing barrier and train",
        "railroad crossing warning lights locomotive at the rails",
    ]

    def one(query: str) -> list[dict[str, Any]]:
        try:
            token = _login()
            r = requests.post(
                f"{VSS_URL}/api/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "query": query,
                    "top_k": 6,
                    "llm_top_n": 1,
                    "min_similarity": MIN_SIMILARITY,
                    "include_public": True,
                    "time_filter": "all",
                },
                timeout=12,
            )
            if r.status_code >= 400:
                return []
            return _collect_matches(r.json(), exclude=set(), top_k=6)
        except Exception:
            return []

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        groups = pool.map(one, queries)
    for group in groups:
        for match in group:
            src = match.get("source") or ""
            if not src or src in seen or not _archive_is_level_crossing(match):
                continue
            seen.add(src)
            found.append(match)
    if found:
        return found
    # The first pass was the wrong place (streets, highway, warehouse). Ask once more,
    # tighter, and still refuse a non-railway hit.
    tighter = one("locomotive passing a railroad grade crossing with the barrier down")
    for match in tighter:
        src = match.get("source") or ""
        if src and src not in seen and _archive_is_level_crossing(match):
            found.append(match)
    return found


def _commons_api(params: dict[str, Any]) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                COMMONS_API,
                params={"format": "json", "formatversion": "2", **params},
                headers={"User-Agent": COMMONS_UA},
                timeout=25,
            )
            if resp.status_code == 429:
                time.sleep(1.2 * (attempt + 1))
                last = RuntimeError("Commons rate limited")
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(0.8 * (attempt + 1))
    raise LevelCrossingFootageError(
        "Level crossing film stopped: Wikimedia Commons is unreachable "
        f"({type(last).__name__ if last else 'error'}). "
        "No street crosswalk or warehouse clip was used."
    )


def _commons_meta_text(meta: dict[str, Any], key: str) -> str:
    return _plain_text(_extmeta(meta, key))


def _commons_clip_record(title: str, info: dict[str, Any]) -> dict[str, Any] | None:
    mime = str(info.get("mime") or "").lower()
    mediatype = str(info.get("mediatype") or "").upper()
    if mime.startswith("audio/") or mediatype == "IMAGE":
        return None
    if mediatype not in {"VIDEO", ""} and not mime.startswith("video/") and mime not in {"application/ogg", "video/ogg"}:
        return None
    try:
        size = int(info.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0 or size > MAX_CC_BYTES:
        return None
    try:
        dur = float(info["duration"]) if info.get("duration") is not None else None
    except (TypeError, ValueError):
        dur = None
    if dur is not None and (dur > MAX_RAIL_CC_SECONDS or dur < 0.8):
        return None
    meta = info.get("extmetadata") or {}
    short = _commons_meta_text(meta, "LicenseShortName")
    usage = _commons_meta_text(meta, "UsageTerms") or _commons_meta_text(meta, "License")
    lic_url = _commons_meta_text(meta, "LicenseUrl")
    desc = _commons_meta_text(meta, "ImageDescription")
    pretty = title.split(":", 1)[1].strip() if title.lower().startswith("file:") else title
    if not _license_ok(short, usage, lic_url):
        return None
    if not _text_is_railway(pretty, desc):
        return None
    url = info.get("url") or ""
    if not url:
        return None
    if not lic_url:
        if re.search(r"public domain|cc0", short, re.I):
            lic_url = "https://creativecommons.org/publicdomain/zero/1.0/"
        else:
            lic_url = "https://commons.wikimedia.org/wiki/File:" + quote(pretty.replace(" ", "_"))
    author = _commons_meta_text(meta, "Artist") or _commons_meta_text(meta, "Credit") or "unknown"
    ext = {
        "video/webm": ".webm",
        "video/mp4": ".mp4",
        "video/ogg": ".ogv",
        "application/ogg": ".ogv",
        "video/x-matroska": ".mkv",
    }.get(mime, Path(url.split("?", 1)[0]).suffix or ".bin")
    return {
        "title": pretty[:180],
        "author": author[:180],
        "license": short or usage or "Creative Commons",
        "license_url": lic_url,
        "url": url,
        "ext": ext,
        "size": size,
    }


def _commons_lookup_titles(titles: list[str]) -> dict[str, dict[str, Any]]:
    if not titles:
        return {}
    data = _commons_api({
        "action": "query",
        "titles": "|".join(titles),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|mediatype|extmetadata|canonicaltitle",
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|UsageTerms|License|ImageDescription",
    })
    found: dict[str, dict[str, Any]] = {}
    for page in (data.get("query") or {}).get("pages") or []:
        if page.get("missing"):
            continue
        title = str(page.get("title") or "")
        info = (page.get("imageinfo") or [None])[0] or {}
        record = _commons_clip_record(title, info)
        if record:
            found[title] = record
    return found


def _commons_search_railway(query: str, used: set[str]) -> dict[str, Any] | None:
    data = _commons_api({
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|mediatype|extmetadata|canonicaltitle",
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|UsageTerms|License|ImageDescription",
    })
    for page in (data.get("query") or {}).get("pages") or []:
        title = str(page.get("title") or "")
        pretty = title.split(":", 1)[1].strip() if title.lower().startswith("file:") else title
        if pretty in used or title in used:
            continue
        info = (page.get("imageinfo") or [None])[0] or {}
        record = _commons_clip_record(title, info)
        if record and record["title"] not in used:
            return record
    return None


def _download_commons_record(record: dict[str, Any], dest_dir: Path) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:10]}{record['ext']}"
    _download_capped(record["url"], dest)
    try:
        got = _probe_duration(dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"could not read duration ({type(exc).__name__})") from exc
    if got > MAX_RAIL_CC_SECONDS or got < 0.5:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"clip duration {got:.1f}s is outside 0.5–{MAX_RAIL_CC_SECONDS:.0f}s")
    return {
        "path": str(dest),
        "title": record["title"],
        "author": record["author"],
        "license": record["license"],
        "license_url": record["license_url"],
    }


def _pick_level_crossing_clips(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One distinct real level-crossing clip per beat. Never a street crosswalk."""
    titles: list[str] = []
    for shot in shots:
        for name in shot.get("commons_files") or []:
            if name not in titles:
                titles.append(name)
    try:
        catalog = _commons_lookup_titles(titles)
    except LevelCrossingFootageError:
        raise
    except Exception as exc:
        raise LevelCrossingFootageError(
            "Level crossing film stopped: Wikimedia Commons failed "
            f"({type(exc).__name__}). No street crosswalk or warehouse clip was used."
        ) from exc

    chosen: list[dict[str, Any] | None] = [None] * len(shots)
    used: set[str] = set()
    for i, shot in enumerate(shots):
        for name in shot.get("commons_files") or []:
            record = catalog.get(name)
            if not record or record["title"] in used:
                continue
            chosen[i] = record
            used.add(record["title"])
            break
    for i, shot in enumerate(shots):
        if chosen[i]:
            continue
        last_err = "no railway clip in the first search"
        for query in shot.get("commons_queries") or []:
            try:
                record = _commons_search_railway(query, used)
            except LevelCrossingFootageError:
                raise
            except Exception as exc:
                last_err = f"Commons search failed ({type(exc).__name__})"
                continue
            if record:
                chosen[i] = record
                used.add(record["title"])
                break
            last_err = f"tighter search “{query}” returned no railway clip"
        if not chosen[i]:
            raise LevelCrossingFootageError(
                "Level crossing film stopped: Commons had no real railway clip for "
                f"“{shot.get('title') or 'a beat'}” ({last_err}). "
                "A city crosswalk was not used."
            )

    dest_dir = CUTS_DIR / "cc-incoming"

    def fetch(record: dict[str, Any]) -> dict[str, Any]:
        return _download_commons_record(record, dest_dir)

    clips: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch, record) for record in chosen if record]
        for shot, record, future in zip(shots, chosen, futures):
            try:
                clips.append(future.result())
            except Exception as exc:
                raise LevelCrossingFootageError(
                    "Level crossing film stopped: could not download the Commons clip "
                    f"“{(record or {}).get('title') or shot.get('title')}” ({exc}). "
                    "No street crosswalk was substituted."
                ) from exc
    if len(clips) != len(shots):
        raise LevelCrossingFootageError(
            "Level crossing film stopped: a beat was left without railway footage."
        )
    return clips


def _assemble_level_crossing() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shots = _level_crossing_shots()
    archive = _search_level_crossing_archive()
    archive = [m for m in archive if _archive_is_level_crossing(m)]
    # Prefer Commons for every beat when the archive has no railway. If it does,
    # use those hits first and only then fill the remaining beats from Commons.
    cc_needed = shots[len(archive):]
    cc_clips = _pick_level_crossing_clips(cc_needed) if cc_needed else []
    enriched: list[dict[str, Any]] = []
    ordered: list[dict[str, Any]] = []
    for i, shot in enumerate(shots):
        clean = {k: v for k, v in shot.items() if k not in {"commons_files", "commons_queries"}}
        if i < len(archive):
            match = archive[i]
            enriched.append({**clean, "matches": [match], "footage": "archive"})
            ordered.append({
                "source": match.get("source") or "",
                "title": shot["title"],
                "filename": match.get("filename") or "",
                "location": match.get("location") or "",
                "camera_id": match.get("camera_id") or "",
                "capture_type": match.get("capture_type") or "",
                "origin": "archive",
            })
            continue
        clip = cc_clips[i - len(archive)]
        credit = {
            "title": clip["title"],
            "author": clip["author"],
            "license": clip["license"],
            "license_url": clip["license_url"],
        }
        note = (
            f"CC B-roll: {credit['title']} · {credit['author']} · {credit['license']} · {credit['license_url']}"
        )
        synthetic = {
            "source": "",
            "filename": clip["title"],
            "location": "wikimedia-commons",
            "camera_id": "",
            "capture_type": "level_crossing",
            "reasoning": note,
            "similarity_score": None,
            "playback_url": "",
            "cc": credit,
        }
        enriched.append({**clean, "matches": [synthetic], "footage": "commons"})
        ordered.append({
            "source": "",
            "local_path": clip["path"],
            "title": shot["title"],
            "filename": clip["title"],
            "location": "wikimedia-commons",
            "camera_id": "",
            "capture_type": "level_crossing",
            "origin": "commons",
            "cc": credit,
        })
    return enriched, ordered


def _caption_windows(lines: list[str], target: float) -> list[tuple[float, float, str]]:
    if not lines or target <= 0:
        return []
    step = target / len(lines)
    windows: list[tuple[float, float, str]] = []
    for i, line in enumerate(lines):
        start = i * step
        end = target if i == len(lines) - 1 else (i + 1) * step
        windows.append((start, end, line))
    return windows


def _canonical_voice(voice: str | None) -> str | None:
    key = (voice or "").strip()
    if not key or key == "off":
        return None
    if key in NEURAL_VOICES:
        return key
    return VOICE_ALIASES.get(key)


def _edge_tts_mp3(text: str, neural: str, dest: Path) -> None:
    import edge_tts

    async def _save() -> None:
        comm = edge_tts.Communicate(text, neural)
        await asyncio.wait_for(comm.save(str(dest)), timeout=30)

    asyncio.run(_save())
    if not dest.is_file() or dest.stat().st_size < 200:
        raise RuntimeError("edge-tts wrote no audio")


def _espeak_wav(text: str, espeak_voice: str, dest: Path) -> None:
    """Fallback only. The mixed track uses this when the neural voice cannot be reached."""
    proc = subprocess.run(
        ["espeak-ng", "-v", espeak_voice, "-s", "150", "-w", str(dest), text],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not dest.is_file():
        raise RuntimeError((proc.stderr or "espeak-ng failed")[-400:])


def _fit_to_window(src: Path, dest: Path, window: float) -> float:
    """Pad one line with silence so it occupies the caption window.

    If the speech is longer than the window, speed it up just enough to finish
    inside that window, then trim. It does not run into the next caption.
    """
    window = max(0.35, float(window))
    dur = _probe_duration(src)
    chain = []
    budget = window * 0.92
    if dur > budget > 0:
        tempo = min(1.85, dur / budget)
        chain.append(f"atempo={tempo:.4f}")
        dur = dur / tempo
    chain.append(f"apad=whole_dur={window:.3f}")
    chain.append(f"atrim=0:{window:.3f}")
    chain.append("asetpts=PTS-STARTPTS")
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", str(src),
        "-af", ",".join(chain),
        "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
        str(dest),
    ])
    return dur


def _vo_line_parts(
    windows: list[tuple[float, float, str]],
    neural: str,
    work: Path,
    *,
    engine: str,
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    espeak_voice = NEURAL_VOICES.get(neural, "en-us+f3")
    for i, (start, end, line) in enumerate(windows):
        window = end - start
        if engine == "edge-tts":
            raw = work / f"line_{i}_raw.mp3"
            _edge_tts_mp3(line, neural, raw)
        else:
            raw = work / f"line_{i}_raw.wav"
            _espeak_wav(line, espeak_voice, raw)
        part = work / f"line_{i}.wav"
        speech = _fit_to_window(raw, part, window)
        parts.append({
            "path": part,
            "line": line[:180],
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "speech_sec": round(min(speech, window), 3),
            "fitted": speech <= window + 0.05,
            "engine": engine,
        })
    return parts


def _plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _cc_keywords(query: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", query or "")
    kept = [w for w in words if w.lower() not in _CC_STOP]
    return " ".join(kept[:8]) or "city street"


def _license_ok(short: str, usage: str, url: str) -> bool:
    blob = " ".join([short or "", usage or "", url or ""])
    if _CC_BAD_RE.search(blob) and not _CC_OK_RE.search(blob):
        return False
    return bool(_CC_OK_RE.search(blob))


def _extmeta(meta: dict[str, Any], key: str) -> str:
    item = meta.get(key) or {}
    if isinstance(item, dict):
        return str(item.get("value") or "")
    return str(item or "")


def _download_capped(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": COMMONS_UA}) as resp:
        resp.raise_for_status()
        total = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_CC_BYTES:
                    raise RuntimeError("over 40MB")
                fh.write(chunk)


def _fetch_cc_clip(query: str, dest_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    """One short CC or public-domain Commons video, or a note explaining the skip."""
    try:
        keywords = _cc_keywords(query)
        resp = requests.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": f"{keywords} filetype:video",
                "gsrnamespace": 6,
                "gsrlimit": 8,
                "prop": "imageinfo",
                "iiprop": "url|size|mime|mediatype|extmetadata|canonicaltitle",
                "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|UsageTerms|License",
            },
            headers={"User-Agent": COMMONS_UA},
            timeout=25,
        )
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or []
    except requests.RequestException as exc:
        return None, (
            "Creative Commons B-roll skipped: Commons unreachable "
            f"({type(exc).__name__}). Rendered from the archive."
        )
    except Exception as exc:
        return None, (
            "Creative Commons B-roll skipped: "
            f"{type(exc).__name__}. Rendered from the archive."
        )
    if not pages:
        return None, "Creative Commons B-roll skipped: Commons returned no video. Rendered from the archive."

    dest_dir.mkdir(parents=True, exist_ok=True)
    saw_network_failure = False
    for page in pages:
        info = (page.get("imageinfo") or [None])[0] or {}
        mime = str(info.get("mime") or "").lower()
        mediatype = str(info.get("mediatype") or "").upper()
        if mime.startswith("audio/"):
            continue
        if mediatype not in {"VIDEO", ""} and not mime.startswith("video/") and mime != "application/ogg":
            continue
        meta = info.get("extmetadata") or {}
        short = _plain_text(_extmeta(meta, "LicenseShortName"))
        usage = _plain_text(_extmeta(meta, "UsageTerms") or _extmeta(meta, "License"))
        lic_url = _plain_text(_extmeta(meta, "LicenseUrl"))
        if not _license_ok(short, usage, lic_url):
            continue
        try:
            size = int(info.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > MAX_CC_BYTES:
            continue
        dur = info.get("duration")
        try:
            dur_f = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur_f = None
        if dur_f is not None and (dur_f > MAX_CC_SECONDS or dur_f < 1):
            continue
        url = info.get("url") or ""
        if not url:
            continue
        title = str(page.get("title") or "Commons video")
        if title.lower().startswith("file:"):
            title = title.split(":", 1)[1].strip()
        ext = {
            "video/webm": ".webm",
            "video/mp4": ".mp4",
            "video/ogg": ".ogv",
            "application/ogg": ".ogv",
            "video/x-matroska": ".mkv",
        }.get(mime, Path(url.split("?", 1)[0]).suffix or ".bin")
        dest = dest_dir / f"{uuid.uuid4().hex[:10]}{ext}"
        try:
            _download_capped(url, dest)
        except requests.RequestException:
            dest.unlink(missing_ok=True)
            saw_network_failure = True
            continue
        except RuntimeError:
            dest.unlink(missing_ok=True)
            continue
        try:
            got = _probe_duration(dest)
        except Exception:
            dest.unlink(missing_ok=True)
            continue
        if got > MAX_CC_SECONDS or got < 0.5:
            dest.unlink(missing_ok=True)
            continue
        author = _plain_text(_extmeta(meta, "Artist") or _extmeta(meta, "Credit")) or "unknown"
        if not lic_url:
            if re.search(r"public domain", short, re.I):
                lic_url = "https://creativecommons.org/publicdomain/mark/1.0/"
            else:
                lic_url = "https://commons.wikimedia.org/wiki/File:" + quote(title.replace(" ", "_"))
        return {
            "path": str(dest),
            "title": title[:180],
            "author": author[:180],
            "license": short or usage or "Creative Commons",
            "license_url": lic_url,
        }, None
    if saw_network_failure:
        return None, "Creative Commons B-roll skipped: Commons unreachable. Rendered from the archive."
    return None, (
        "Creative Commons B-roll skipped: no short CC or public-domain video under 40MB. "
        "Rendered from the archive."
    )


def _render_same_length_cut(
    source_clips: list[dict[str, Any]],
    match_sources: list[str],
    transcript: str,
    voice: str | None = DEFAULT_VOICE,
    bed: float = 0.18,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """Concat archive matches and keep their audio. Speak each VO line inside its caption window."""
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in match_sources:
        if isinstance(item, str):
            item = {"source": item, "title": "", "filename": item.rsplit("/", 1)[-1]}
        src = item.get("source") or ""
        local = item.get("local_path") or ""
        key = src or local
        if not key or key in seen:
            continue
        seen.add(key)
        picks.append(item)
        if len(picks) >= 4:
            break
    if not picks:
        raise RuntimeError("no archive matches to cut together")

    cut_id = uuid.uuid4().hex[:12]
    work = CUTS_DIR / cut_id
    work.mkdir(parents=True, exist_ok=True)

    target = 0.0
    if duration_sec and duration_sec >= 4:
        target = min(90.0, float(duration_sec))
    else:
        for i, clip in enumerate(source_clips or []):
            raw = work / f"mine_{i}.mp4"
            _download_mp4(clip["source"], raw)
            target += _probe_duration(raw)
        if target < 0.5:
            target = 4.0 * len(picks)

    slice_len = target / len(picks)
    timeline: list[dict[str, Any]] = []
    trimmed: list[Path] = []
    for i, item in enumerate(picks):
        start = round(i * slice_len, 2)
        end = round(target if i == len(picks) - 1 else (i + 1) * slice_len, 2)
        src = item.get("source") or ""
        row: dict[str, Any] = {
            "title": item.get("title") or f"Shot {i + 1}",
            "filename": item.get("filename") or (src.rsplit("/", 1)[-1] if src else ""),
            "start_sec": start,
            "end_sec": end,
        }
        if item.get("cc"):
            row["cc"] = item["cc"]
            credit = item["cc"]
            row["note"] = (
                f"CC B-roll: {credit.get('title') or row['filename']}"
                f" · {credit.get('author') or 'unknown'}"
                f" · {credit.get('license') or 'CC'}"
                f" · {credit.get('license_url') or ''}"
            )
        timeline.append(row)
        local = item.get("local_path") or ""
        suffix = Path(local or src).suffix or ".mp4"
        raw = work / f"raw_{i}{suffix}"
        if local:
            raw.write_bytes(Path(local).read_bytes())
        else:
            _download_mp4(src, raw)
        norm = work / f"norm_{i}.mp4"
        scale = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24"
        if _has_audio(raw):
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(raw), "-vf", scale,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                str(norm),
            ])
        else:
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(raw),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest",
                "-vf", scale, "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                str(norm),
            ])
        out = work / f"trim_{i}.mp4"
        dur = _probe_duration(norm)
        cmd = ["ffmpeg", "-y"]
        if dur + 0.05 < slice_len:
            cmd += ["-stream_loop", "-1"]
        cmd += [
            "-i", str(norm), "-t", f"{slice_len:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            str(out),
        ]
        _run_ffmpeg(cmd)
        trimmed.append(out)

    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in trimmed), encoding="utf-8")
    concat = work / "concat.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(concat),
    ])

    vf = f"trim=duration={target:.3f},setpts=PTS-STARTPTS"
    lines = [ln.strip() for ln in re.split(r"\n+", transcript or "") if ln.strip()]
    windows = _caption_windows(lines, target)
    vo_windows: list[dict[str, Any]] = []
    if windows:
        blocks = []
        for i, (start, end, line) in enumerate(windows):
            blocks.append(f"{i+1}\n{_srt_time(start)} --> {_srt_time(end)}\n{line}\n")
        (work / "vo.srt").write_text("\n".join(blocks), encoding="utf-8")
        vf += (
            ",subtitles=vo.srt:force_style="
            "'FontName=DejaVu Sans,FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2'"
        )

    neural = _canonical_voice(voice)
    spoken = bool(neural) and bool(windows)
    voice_note = None
    voice_engine = None
    if spoken and neural:
        try:
            built = _vo_line_parts(windows, neural, work, engine="edge-tts")
            voice_engine = "edge-tts"
        except Exception as exc:
            voice_note = (
                f"Neural voice unavailable ({type(exc).__name__}). Used an espeak fallback."
            )
            try:
                built = _vo_line_parts(windows, neural, work, engine="espeak")
                voice_engine = "espeak"
            except Exception as exc2:
                spoken = False
                built = []
                voice_engine = "off"
                voice_note = (
                    f"Voiceover skipped: neural voice failed ({type(exc).__name__}) "
                    f"and espeak failed ({type(exc2).__name__})."
                )
        if spoken and built:
            vo_windows = [
                {k: v for k, v in info.items() if k != "path"} for info in built
            ]
            listing_vo = work / "vo_list.txt"
            listing_vo.write_text(
                "".join(f"file '{info['path'].resolve()}'\n" for info in built),
                encoding="utf-8",
            )
            wav = work / "vo.wav"
            _run_ffmpeg([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing_vo),
                "-c", "copy", str(wav),
            ])
            bed_v = min(0.8, max(0.05, float(bed)))
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(concat), "-i", str(wav),
                "-filter_complex",
                (
                    f"[0:a]volume={bed_v:.2f}[bed];"
                    "[1:a]volume=1.15[vo];"
                    "[bed][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                    "alimiter=limit=0.95[a]"
                ),
                "-map", "0:v", "-map", "[a]",
                "-vf", vf, "-t", f"{target:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                str(work / "cut.mp4"),
            ], cwd=work)
        else:
            spoken = False
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(concat),
                "-vf", vf, "-t", f"{target:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                str(work / "cut.mp4"),
            ], cwd=work)
    else:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(concat),
            "-vf", vf, "-t", f"{target:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            str(work / "cut.mp4"),
        ], cwd=work)
    return {
        "id": cut_id,
        "url": f"{APP_PREFIX}/api/cuts/{cut_id}",
        "target_duration_sec": round(target, 2),
        "pieces": len(trimmed),
        "vo": spoken,
        "voice": neural if spoken else "off",
        "voice_engine": voice_engine if spoken else "off",
        "voice_note": voice_note,
        "bed": bed,
        "timeline": timeline,
        "vo_windows": vo_windows,
    }


FEEDBACK_PATH = Path("/tmp/idea-feedback.jsonl")


@app.post("/api/feedback")
def save_feedback():
    payload = request.get_json(force=True, silent=True) or {}
    verdict = payload.get("verdict") or ""
    if verdict not in ("works", "close", "wrong"):
        verdict = ""
    clips = []
    for item in (payload.get("clips") or [])[:8]:
        if not isinstance(item, dict):
            continue
        raw_mark = item.get("mark")
        mark = "like" if raw_mark in ("like", "keep") else "no" if raw_mark in ("no", "drop") else ""
        clips.append({
            "title": str(item.get("title") or "")[:80],
            "filename": str(item.get("filename") or "")[:120],
            "start_sec": item.get("start_sec"),
            "end_sec": item.get("end_sec"),
            "mark": mark,
        })
    row = {
        "at": time.time(),
        "cut_id": str(payload.get("cut_id") or "")[:16],
        "verdict": verdict,
        "note": str(payload.get("note") or "").strip()[:500],
        "clips": clips,
    }
    if not row["note"] and not any(c["mark"] for c in clips):
        return jsonify({"error": "say which shots you like"}), 400
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return jsonify({"ok": True})


@app.get("/api/cuts/<cut_id>")
def serve_cut(cut_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", cut_id or ""):
        return jsonify({"error": "bad id"}), 400
    path = CUTS_DIR / cut_id / "cut.mp4"
    if not path.is_file():
        return jsonify({"error": "cut not ready"}), 404
    return Response(path.read_bytes(), mimetype="video/mp4")


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
        raw_voice = (payload.get("voice") or DEFAULT_VOICE).strip()
        voice = None if raw_voice == "off" else (_canonical_voice(raw_voice) or DEFAULT_VOICE)
        try:
            bed = float(payload.get("bed") if payload.get("bed") is not None else 0.18)
        except (TypeError, ValueError):
            bed = 0.18
        health_safety = bool(payload.get("health_safety"))
        cc_broll = bool(payload.get("cc_broll"))
        duration_sec = None
        if payload.get("duration_sec") not in (None, "", 0, "0"):
            try:
                duration_sec = float(payload.get("duration_sec"))
            except (TypeError, ValueError):
                return jsonify({"error": "cut length must be a number of seconds"}), 400
            if duration_sec < 4 or duration_sec > 90:
                return jsonify({"error": "cut length must be between 4 and 90 seconds"}), 400
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

        if health_safety:
            splitter = "health_safety"
            shots = _health_safety_shots()
        else:
            splitter = "wandb"
            shots = _wandb_shots(logline, beats, transcript)
            if not shots:
                splitter = "heuristic"
                shots = _heuristic_shots(logline, beats, transcript)
        shots = shots[:4]

        # Bias queries toward cinematic archive when seeding from a rough upload.
        # Health & safety keeps its own single-line queries.
        if seed_from_upload and not health_safety:
            for shot in shots:
                q = shot.get("search_query") or ""
                shot["search_query"] = (
                    f"cinematic polished production value - {q} - "
                    "urban streets traffic pedestrians vehicles dramatic light"
                )

        enriched = []
        used_sources = set(exclude)
        for shot in shots:
            try:
                matches = _search_shot(shot, exclude_sources=used_sources)
            except Exception as e:
                matches = []
                shot = {**shot, "search_error": str(e)}
            if matches and matches[0].get("source"):
                used_sources.add(matches[0]["source"])
            enriched.append({**shot, "matches": matches})

        directors_cut = []
        ordered: list[dict[str, Any] | None] = []
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
            if best and best.get("source"):
                ordered.append({
                    "source": best["source"],
                    "title": shot.get("title") or "",
                    "filename": best.get("filename") or "",
                })
            else:
                ordered.append(None)

        cc_note = None
        cc_meta = None
        if cc_broll:
            spoken = {
                ln.strip().lower()
                for ln in re.split(r"\n+", transcript or "")
                if ln.strip()
            }
            weak_rows: list[tuple[float, int, bool]] = []
            for i, item in enumerate(ordered):
                score = None
                matches = enriched[i].get("matches") or []
                if matches:
                    try:
                        score = float(matches[0].get("similarity_score"))
                    except (TypeError, ValueError):
                        score = None
                weak = item is None or (score is not None and score < CC_GAP_BELOW)
                if not weak:
                    continue
                phrase = (
                    enriched[i].get("action")
                    or enriched[i].get("intent")
                    or ""
                ).strip().lower()
                rank = -1.0 if item is None or score is None else score
                weak_rows.append((rank, i, phrase in spoken))
            visual = [row for row in weak_rows if not row[2]]
            pool = visual or weak_rows
            pool.sort()
            gap = pool[0][1] if pool else None
            if gap is None:
                cc_note = "Archive already had a shot for every beat, so no Commons clip was used."
            else:
                phrase = (
                    enriched[gap].get("action")
                    or enriched[gap].get("intent")
                    or ""
                ).strip()
                if phrase.lower() in spoken:
                    query = logline or phrase or "city street"
                else:
                    query = phrase or enriched[gap].get("search_query") or logline or "city street"
                clip, note = _fetch_cc_clip(query, CUTS_DIR / "cc-incoming")
                if clip:
                    cc_meta = {
                        "title": clip["title"],
                        "author": clip["author"],
                        "license": clip["license"],
                        "license_url": clip["license_url"],
                    }
                    ordered[gap] = {
                        "source": "",
                        "local_path": clip["path"],
                        "title": enriched[gap].get("title") or "B-roll",
                        "filename": clip["title"],
                        "cc": cc_meta,
                    }
                    cc_note = (
                        f"CC B-roll: {clip['title']} · {clip['author']} · {clip['license']}"
                    )
                else:
                    cc_note = note
        match_sources = [item for item in ordered if item]

        rendered = None
        render_error = None
        if match_sources:
            try:
                rendered = _render_same_length_cut(
                    (source_upload or {}).get("clips") or [],
                    match_sources,
                    transcript,
                    voice,
                    bed,
                    duration_sec,
                )
            except Exception as e:
                render_error = str(e)
        if rendered is not None:
            bits = [rendered.get("voice_note"), cc_note]
            merged = " ".join(bit for bit in bits if bit)
            if merged:
                rendered["note"] = merged
            if cc_meta:
                rendered["cc"] = cc_meta
        elif cc_note:
            if render_error:
                render_error = f"{render_error} {cc_note}"
            else:
                render_error = cc_note

        return jsonify(
            {
                "splitter": splitter,
                "health_safety": health_safety,
                "cc_broll": cc_broll,
                "logline": logline,
                "seed_from_upload": seed_from_upload,
                "upload_tag": upload_tag if seed_from_upload else None,
                "source_upload": source_upload,
                "vss_url_host": VSS_URL.split("://")[-1].split("/")[0],
                "shots": enriched,
                "directors_cut": directors_cut,
                "rendered_cut": rendered,
                "render_error": render_error,
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
  .wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 72px; }
  header h1 {
    font-family: Fraunces, Georgia, serif;
    font-size: clamp(2.4rem, 5vw, 3.6rem);
    font-weight: 500;
    margin: 6px 0 8px;
    letter-spacing: -0.03em;
    line-height: 0.95;
  }
  header p.lead { margin: 0 0 28px; color: var(--muted); max-width: 38rem; line-height: 1.5; }
  .kicker { margin: 0; font-size: 0.75rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); }
  .grid { display: grid; gap: 16px; }
  @media (min-width: 980px) {
    .grid.compose { grid-template-columns: 1.05fr 0.95fr; align-items: start; }
    .grid.results { grid-template-columns: 240px 1fr; }
  }
  label { display: block; font-size: 0.68rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
  select, input[type=number], input[type=text] {
    font: inherit; width: 100%; padding: 10px 12px; border: 1px solid var(--line);
    background: var(--bg); color: var(--ink); border-radius: 10px;
  }
  textarea {
    width: 100%; border: 1px solid var(--line); background: var(--bg); color: var(--ink);
    border-radius: 12px; padding: 12px 14px; font: inherit; resize: vertical; min-height: 76px;
  }
  textarea:focus, select:focus, input:focus { outline: 2px solid color-mix(in srgb, var(--accent) 55%, transparent); border-color: transparent; }
  .controls { display: grid; gap: 10px; margin-top: 14px; }
  @media (min-width: 720px) { .controls { grid-template-columns: 1fr 1fr; } }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; align-items: center; }
  button {
    font: inherit; font-weight: 600; border: none; cursor: pointer;
    background: var(--accent); color: var(--accent-ink); padding: 11px 16px; border-radius: 999px;
  }
  button:disabled { opacity: 0.45; cursor: wait; }
  button.ghost, button.chip {
    background: transparent; color: var(--ink); border: 1px solid var(--line); border-radius: 999px;
    padding: 8px 12px; font-weight: 500;
  }
  button.chip:hover, button.ghost:hover { border-color: var(--accent); color: var(--accent); }
  button.chip.health { border-color: var(--accent); color: var(--accent); }
  .status { font-size: 0.88rem; color: var(--muted); }
  .status.err { color: #ffb4a8; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; }
  .shot-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .shot-list button {
    width: 100%; text-align: left; background: transparent; color: var(--ink);
    border: 1px solid transparent; border-radius: 12px; padding: 10px 12px;
  }
  .shot-list button:hover { border-color: var(--line); background: var(--panel-2); }
  .shot-list button.active { border-color: var(--accent); background: var(--panel-2); }
  .shot-list .meta { display: block; font-size: 0.75rem; color: var(--muted); margin-top: 2px; font-weight: 400; }
  .match { border-top: 1px solid var(--line); padding: 14px 0; display: grid; gap: 12px; }
  @media (min-width: 700px) { .match { grid-template-columns: 280px 1fr; } }
  video { width: 100%; background: #000; border-radius: 14px; aspect-ratio: 16/9; }
  .score { color: var(--accent); font-weight: 600; font-size: 0.85rem; }
  .reason { font-size: 0.92rem; line-height: 1.45; }
  .cut ol { margin: 8px 0 0; padding-left: 1.1rem; }
  .cut li { margin-bottom: 8px; line-height: 1.4; }
  .tag { display: inline-block; font-size: 0.72rem; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); margin-right: 4px; color: var(--muted); }
  h2 { font-family: Fraunces, Georgia, serif; font-weight: 500; font-size: 1.6rem; margin: 0 0 12px; }
  .hidden { display: none !important; }
  .progress { margin: 14px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; color: var(--muted); font-size: 0.88rem; }
  .progress li.done { color: var(--good); }
  .progress li.now { color: var(--accent); }
  .opt { margin-top: 14px; padding: 12px; border-radius: 14px; background: var(--panel-2); display: grid; gap: 8px; }
  .opt label.row {
    display: flex; align-items: center; gap: 10px; text-transform: none;
    font-size: 0.95rem; font-weight: 500; letter-spacing: 0; color: var(--ink); cursor: pointer; margin: 0;
  }
  .opt .hint { font-size: 0.82rem; color: var(--muted); line-height: 1.4; margin: 0; }
  .tl { display: flex; width: 100%; min-height: 52px; margin-top: 12px; gap: 4px; }
  .tl button {
    flex: 1 1 0; min-width: 0; border: none; border-radius: 10px; color: #f7f3ea;
    font-size: 0.78rem; font-weight: 600; padding: 8px 8px; text-align: left; overflow: hidden;
    line-height: 1.25; text-shadow: 0 1px 2px rgba(0,0,0,.55);
  }
  .tl button small { display: block; font-weight: 500; opacity: 0.95; }
  .fb { margin-top: 14px; display: grid; gap: 8px; }
  .marks { display: flex; flex-direction: column; gap: 8px; }
  .shot-fb { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .shot-fb span { flex: 1 1 160px; font-size: 0.88rem; }
  .shot-fb button.on { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="kicker"><a href="/app/">All apps</a></p>
      <h1>Idea Upscaler</h1>
      <p class="lead">Boards and a voice line, cut from the archive. Streets and traffic are most of the footage. Indoor crowds and the synthetic warehouse are the smaller sets.</p>
    </header>

    <section class="grid compose">
      <div class="panel">
        <label for="logline">Logline / scene intent *</label>
        <textarea id="logline" placeholder="A cyclist threads dusk traffic while a voiceover remembers leaving the city…"></textarea>
        <label for="beats" style="margin-top:12px">Storyboard notes (per beat)</label>
        <textarea id="beats" placeholder="Wide city canyon at dusk&#10;Bike lane tracking, yellow jacket ahead&#10;Crosswalk pause, phone glow&#10;Night close on helmet reflection"></textarea>
        <label for="transcript" style="margin-top:12px">Director VO / dialogue (optional)</label>
        <textarea id="transcript" placeholder="I kept the camera rolling… we were already gone before we left."></textarea>
        <div class="controls">
          <div>
            <label for="voice">Voice</label>
            <select id="voice">
              <option value="en-US-JennyNeural">US female</option>
              <option value="en-US-GuyNeural">US male</option>
              <option value="en-GB-SoniaNeural">UK female</option>
              <option value="en-GB-RyanNeural">UK male</option>
              <option value="off">No voiceover</option>
            </select>
          </div>
          <div>
            <label for="bed">Footage audio under the voice</label>
            <select id="bed">
              <option value="0.16" selected>Quiet</option>
              <option value="0.22">Medium</option>
              <option value="0.65">Louder</option>
            </select>
          </div>
          <div>
            <label for="lengthMode">Cut length</label>
            <select id="lengthMode">
              <option value="auto">Automatic</option>
              <option value="custom">Custom seconds</option>
            </select>
          </div>
          <div id="lengthWrap" class="hidden">
            <label for="lengthSec">Seconds (4–90)</label>
            <input id="lengthSec" type="number" min="4" max="90" step="1" value="15"/>
          </div>
        </div>
        <div class="opt">
          <label class="row" for="seed">
            <input id="seed" type="checkbox"/>
            Upscale my upload (tag)
          </label>
          <p class="hint">Reads your indexed clip (default tag <code>shittyvideo</code>) and searches the archive for a cinematic match. Mars/fantasy only works if similar footage is already indexed.</p>
          <input id="tag" type="text" value="shittyvideo" placeholder="tag"/>
        </div>
        <div class="opt">
          <label class="row" for="ccbroll">
            <input id="ccbroll" type="checkbox"/>
            CC B-roll
          </label>
          <p class="hint">If a beat is missing from the archive, add one short Creative Commons or public-domain clip from Wikimedia Commons. The timeline keeps the title, author, and license. If Commons is unreachable, the cut still uses the archive.</p>
        </div>
        <ol class="progress hidden" id="progress"></ol>
        <div class="actions">
          <button id="go" type="button">Upscale idea</button>
          <button id="demo" class="chip" type="button">Streets · NYC bike</button>
          <button id="shitty" class="chip" type="button">My upload</button>
          <span class="status" id="status"></span>
        </div>
        <p class="kicker" style="margin-top:16px">Briefs</p>
        <div class="actions" id="briefs"></div>
      </div>
      <div class="panel cut" id="cut-panel">
        <h2>Director's cut</h2>
        <p class="status" id="cut-empty">Upscale shittyvideo to cut a reel the same length as your clip, with the VO burned in.</p>
        <video id="cut-video" class="hidden" controls playsinline></video>
        <div id="timeline" class="tl hidden"></div>
        <div id="feedback" class="fb hidden">
          <label>I like this shot, not that one</label>
          <div id="marks" class="marks"></div>
          <textarea id="fbNote" placeholder="Optional. Why that shot and not the other."></textarea>
          <div class="actions">
            <button id="fbSend" type="button">Send feedback</button>
            <span class="status" id="fbStatus"></span>
          </div>
        </div>
        <p class="status" id="cut-meta"></p>
        <div id="credits"></div>
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
let verdict = "";

function setProgress(steps, activeIdx) {
  const el = $("progress");
  el.classList.remove("hidden");
  el.innerHTML = steps.map((t, i) => `<li class="${i < activeIdx ? "done" : i === activeIdx ? "now" : ""}">${escapeHtml(t)}</li>`).join("");
}

async function runUpscale({ seed, health }) {
  const logline = $("logline").value.trim();
  const useSeed = seed || $("seed").checked;
  const useHealth = health === true;
  if (!logline && !useSeed) {
    $("status").textContent = "Logline is required, or use Upscale shittyvideo.";
    $("status").className = "status err";
    return;
  }
  const steps = useHealth
    ? ["Reading the safety brief", "Searching near-miss and forklift moments", "Cutting the training reel", "Timing the safety line to the captions"]
    : useSeed
    ? ["Reading your clip", "Finding stand-in shots", "Cutting a reel to your duration", "Burning the voice line"]
    : ["Splitting the brief", "Searching the archive", "Cutting the shots together", "Burning the voice line"];
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
        voice: $("voice").value,
        bed: $("bed").value,
        duration_sec: $("lengthMode").value === "custom" ? Number($("lengthSec").value) : null,
        seed_from_upload: useSeed,
        upload_tag: ($("tag").value || "shittyvideo").trim(),
        health_safety: useHealth,
        cc_broll: $("ccbroll").checked,
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
    const cut = data.rendered_cut;
    if (cut && cut.url) {
      $("status").textContent = `Cut ready · ${cut.target_duration_sec}s`;
    } else if (data.render_error) {
      $("status").className = "status err";
      $("status").textContent = data.render_error;
    } else {
      const hits = (data.shots || []).reduce((n, s) => n + (s.matches || []).length, 0);
      $("status").textContent = `Ready · ${data.splitter} · ${data.shots.length} shots · ${hits} matches`;
    }
  } catch (e) {
    $("status").className = "status err";
    $("status").textContent = String(e.message || e);
  } finally {
    clearInterval(timer);
    $("go").disabled = false;
    $("shitty").disabled = false;
  }
}

const BRIEFS = [
  {
    label: "Streets · NYC bike",
    logline: "Dusk ride through the city. A cyclist in the bike lane, then a quiet stop.",
    beats: "Wide city street at dusk\nCyclist riding in the marked bike lane\nPedestrians at a crosswalk\nCyclist paused on the sidewalk",
    transcript: "We told ourselves it was just another night ride.\nBut the city already knew we were leaving.",
    seconds: 12,
  },
  {
    label: "Health & safety",
    health: true,
    logline: "Spot the hazard before it closes. A pedestrian nears the lane, a person stands in the forklift aisle, then a gap opens that is actually safe.",
    beats: "Pedestrian approaching a marked crosswalk with cars nearby\nPerson standing close to a forklift in a warehouse aisle\nPedestrians crossing the street while cars wait",
    transcript: "Spot the hazard before you step closer.\nThat is a close call, not a gap.\nWait until the lane is clear, then move.",
    seconds: 12,
  },
  {
    label: "Streets · SF crosswalk",
    logline: "People cross a San Francisco street while the cars wait.",
    beats: "Wide city intersection\nTwo people walking in the crosswalk\nCars held at the crossing",
    transcript: "They step off the curb.\nThe street lets them through.",
    seconds: 12,
  },
  {
    label: "Traffic · highway",
    logline: "Overhead highway. Traffic flows both ways, with a truck among the lanes.",
    beats: "Wide multi-lane highway\nTraffic moving in both directions\nA truck holding its lane",
    transcript: "The lanes keep moving.\nNobody brakes.",
    seconds: 12,
  },
  {
    label: "Crowds · indoor",
    logline: "People walk across a bright indoor warehouse floor.",
    beats: "Open indoor floor and white walls\nPeople crossing the space\nThey pass toward the far side",
    transcript: "The room is already busy.\nSomeone crosses through.",
    seconds: 12,
  },
  {
    label: "Warehouse · forklift",
    logline: "A forklift moves down a warehouse aisle and slows near the racks.",
    beats: "Wide view of the warehouse aisle\nForklift driving between the shelves\nIt slows beside stacked goods",
    transcript: "The aisle is clear.\nThen the forklift takes the lane.",
    seconds: 12,
  },
];

function applyBrief(b, run) {
  $("seed").checked = false;
  $("logline").value = b.logline;
  $("beats").value = b.beats;
  $("transcript").value = b.transcript;
  $("lengthMode").value = "custom";
  $("lengthSec").value = String(b.seconds || 12);
  $("lengthWrap").classList.remove("hidden");
  if (run) runUpscale({ seed: false, health: !!b.health });
}

$("briefs").innerHTML = BRIEFS.slice(1).map((b, i) =>
  `<button type="button" class="chip${b.health ? " health" : ""}" data-brief="${i + 1}">${b.label}</button>`
).join("");
$("briefs").querySelectorAll("button").forEach(btn => {
  btn.onclick = () => applyBrief(BRIEFS[Number(btn.dataset.brief)], true);
});

$("demo").onclick = () => applyBrief(BRIEFS[0], true);

$("shitty").onclick = () => {
  $("seed").checked = true;
  $("tag").value = "shittyvideo";
  $("logline").value = "Edit the hand out. Make this rough toy clip feel cinematic — close on the figure, then a wider world around it.";
  $("beats").value = "Close on hands and the small white toy\nPull back to the space around the figure\nMatch the gesture with a cleaner cinematic shot";
  $("transcript").value = "It started as a mess on the table.\nThen the shot found the story.";
  runUpscale({ seed: true });
};

$("lengthMode").onchange = () => {
  $("lengthWrap").classList.toggle("hidden", $("lengthMode").value !== "custom");
};
$("go").onclick = () => runUpscale({ seed: false });

document.querySelectorAll("#feedback [data-v]").forEach(btn => {
  btn.onclick = () => {
    verdict = btn.dataset.v;
    document.querySelectorAll("#feedback [data-v]").forEach(b => b.classList.toggle("on", b === btn));
  };
});
$("fbSend").onclick = async () => {
  const reel = state && state.rendered_cut;
  if (!reel) return;
  const rows = reel.timeline || [];
  const clips = rows.map((row, i) => {
    const on = document.querySelector(`#marks .shot-fb[data-i="${i}"] button.on`);
    return {
      title: row.title,
      filename: row.filename,
      start_sec: row.start_sec,
      end_sec: row.end_sec,
      mark: on ? on.dataset.mark : "",
    };
  });
  $("fbStatus").className = "status";
  $("fbStatus").textContent = "Sending…";
  try {
    const res = await fetch("/app/api/feedback", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        cut_id: reel.id,
        verdict,
        note: $("fbNote").value,
        clips,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    const liked = clips.filter(c => c.mark === "like").map(c => c.title);
    const nope = clips.filter(c => c.mark === "no").map(c => c.title);
    $("fbStatus").textContent = `Saved. Like: ${liked.join(", ") || "none"}. Not this one: ${nope.join(", ") || "none"}.`;
  } catch (e) {
    $("fbStatus").className = "status err";
    $("fbStatus").textContent = String(e.message || e);
  }
};

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

  const reel = state.rendered_cut;
  const vid = $("cut-video");
  if (reel && reel.url) {
    $("cut-empty").classList.add("hidden");
    vid.classList.remove("hidden");
    vid.src = reel.url;
    const colors = ["#0d6e6e", "#c45c26", "#1f6b3a", "#3d4f7c"];
    const tl = $("timeline");
    const rows = reel.timeline || [];
    if (rows.length) {
      tl.classList.remove("hidden");
      tl.innerHTML = rows.map((row, i) => `
        <button type="button" data-t="${row.start_sec}" style="background:${colors[i % colors.length]}" title="${escapeHtml(row.note || row.title)}">
          ${escapeHtml(row.title)}
          <small>${fmtTime(row.start_sec)}–${fmtTime(row.end_sec)}${row.cc ? " · " + escapeHtml(row.cc.license || "CC") : ""}</small>
        </button>`).join("");
      tl.querySelectorAll("button").forEach(btn => {
        btn.onclick = () => { vid.currentTime = Number(btn.dataset.t) || 0; vid.play(); };
      });
    } else {
      tl.classList.add("hidden");
      tl.innerHTML = "";
    }
    const voiceNote = reel.vo ? " · voice lined up with the captions" : " · footage audio only";
    $("cut-meta").textContent = `${reel.target_duration_sec}s · ${reel.pieces} shots` + voiceNote + (reel.note ? " · " + reel.note : "");
    $("credits").innerHTML = rows.filter(row => row.cc).map(row =>
      `<p class="status">CC: ${escapeHtml(row.cc.title)} · ${escapeHtml(row.cc.author || "unknown author")} · ${escapeHtml(row.cc.license || "")} · ${escapeHtml(row.cc.license_url || "")}</p>`
    ).join("");
    const fb = $("feedback");
    fb.classList.remove("hidden");
    $("marks").innerHTML = rows.map((row, i) => `
      <div class="shot-fb" data-i="${i}">
        <span>${escapeHtml(row.title)} ${fmtTime(row.start_sec)}–${fmtTime(row.end_sec)}</span>
        <button type="button" class="ghost" data-mark="like">I like this</button>
        <button type="button" class="ghost" data-mark="no">Not this one</button>
      </div>`).join("");
    $("marks").querySelectorAll("button").forEach(btn => {
      btn.onclick = () => {
        const row = btn.parentElement;
        row.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === btn));
      };
    });
    $("fbStatus").textContent = "";
  } else {
    vid.classList.add("hidden");
    vid.removeAttribute("src");
    $("timeline").classList.add("hidden");
    $("feedback").classList.add("hidden");
    $("credits").innerHTML = "";
    $("cut-meta").textContent = state.render_error || "";
  }
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


def _catalog() -> list[dict[str, Any]]:
    """Buttons on the launcher. A teammate claims the guest slot via apps.json + GUEST_UPSTREAM."""
    path = Path(__file__).with_name("apps.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    guest_upstream = (os.environ.get("GUEST_UPSTREAM") or "").strip().rstrip("/")
    apps = []
    for item in raw.get("apps") or []:
        row = dict(item)
        if row.get("id") == "guest" and guest_upstream:
            row["ready"] = True
            if row.get("live_blurb"):
                row["blurb"] = row["live_blurb"]
        else:
            row["ready"] = bool(row.get("ready", True))
        apps.append(row)
    return apps


LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Team 41 apps</title>
<base href="/app/"/>
<style>
  :root { --bg:#e8e2d6; --ink:#1a1f24; --muted:#5a6570; --panel:#f7f3ea; --line:#c9c0b0; --accent:#0d6e6e; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; font-family: "IBM Plex Sans", system-ui, sans-serif; color:var(--ink);
    background: radial-gradient(ellipse 80% 50% at 10% -10%, #dfecec 0%, transparent 55%), var(--bg); }
  .wrap { max-width: 880px; margin: 0 auto; padding: 48px 20px 64px; }
  h1 { font-family: Georgia, serif; font-size: clamp(2rem, 5vw, 3rem); margin: 0 0 8px; }
  p.lead { color: var(--muted); max-width: 38rem; line-height: 1.45; }
  .grid { display:grid; gap:14px; margin-top: 28px; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }
  a.card { display:block; text-decoration:none; color:inherit; background:var(--panel); border:1px solid var(--line); padding:18px 18px 16px; }
  a.card:hover { border-color: var(--accent); }
  a.card h2 { margin: 0 0 8px; font-size: 1.25rem; }
  a.card p { margin:0; color:var(--muted); line-height:1.4; }
  .pill { display:inline-block; font-size:0.72rem; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:8px; color:var(--accent); }
  .pill.wait { color:#8b1e1e; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>Team 41</h1>
    <p class="lead">One App button on the lab page. Pick a project. The second card is an open slot: another person deploys their app behind it.</p>
    <div class="grid">
      __CARDS__
    </div>
  </div>
</body>
</html>
"""

GUEST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Open slot</title>
<base href="/app/"/>
<style>
  body { margin:0; font-family: system-ui, sans-serif; background:#e8e2d6; color:#1a1f24; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 40px 20px; }
  a { color:#0d6e6e; }
  code, pre { background:#f7f3ea; border:1px solid #c9c0b0; padding:2px 6px; }
  pre { padding:12px; overflow:auto; }
</style>
</head>
<body>
  <div class="wrap">
    <p><a href="/app/">All apps</a></p>
    <h1>This button is free</h1>
    <p>Idea Upscaler owns the other card. Put your app behind this one without taking <code>/app</code>.</p>
    <ol>
      <li>Run your app in namespace <code>team-41</code> (ClusterIP Service).</li>
      <li>Set <code>GUEST_UPSTREAM</code> on the <code>hackathon-app</code> Deployment to that service, for example <code>http://my-app:8080</code>.</li>
      <li>Edit <code>tools/idea-upscaler/apps.json</code>: title, blurb, and <code>live_blurb</code>.</li>
      <li>Recreate the ConfigMap and restart. This card then proxies your app.</li>
    </ol>
    <pre>kubectl -n team-41 set env deploy/hackathon-app GUEST_UPSTREAM=http://YOUR-SERVICE:PORT
kubectl -n team-41 create configmap hackathon-app-code --from-file=tools/idea-upscaler --dry-run=client -o yaml | kubectl apply -f -
kubectl -n team-41 rollout restart deploy/hackathon-app</pre>
    <p>The browser still talks to <code>/app/guest/...</code>. This process forwards that to your Service. Pods cannot use the public Ingress hostname.</p>
  </div>
</body>
</html>
"""


def _launcher_page() -> str:
    cards = []
    for item in _catalog():
        ready = item.get("ready")
        pill = "Ready" if ready else "Open slot"
        pill_class = "pill" if ready else "pill wait"
        href = item.get("href") or "guest"
        title = (item.get("title") or "App").replace("&", "&amp;").replace("<", "&lt;")
        blurb = (item.get("blurb") or "").replace("&", "&amp;").replace("<", "&lt;")
        cards.append(
            f'<a class="card" href="{href}">'
            f'<div class="{pill_class}">{pill}</div>'
            f"<h2>{title}</h2><p>{blurb}</p></a>"
        )
    return LAUNCHER_HTML.replace("__CARDS__", "\n".join(cards))


@app.get("/")
def index():
    return Response(_launcher_page(), mimetype="text/html")


@app.get("/upscaler")
def upscaler():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/guest", defaults={"subpath": ""}, strict_slashes=False, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/guest/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def guest(subpath: str):
    """Proxy a teammate's in-cluster app. Empty slot explains how to claim the button."""
    upstream = (os.environ.get("GUEST_UPSTREAM") or "").strip().rstrip("/")
    if not upstream:
        return Response(GUEST_HTML, mimetype="text/html")
    url = f"{upstream}/{subpath}" if subpath else upstream + "/"
    headers = {
        k: v for k, v in request.headers
        if k.lower() not in {"host", "content-length", "connection"}
    }
    upstream_resp = requests.request(
        request.method,
        url,
        params=request.args,
        data=request.get_data(),
        headers=headers,
        timeout=60,
        allow_redirects=False,
    )
    hop = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    out_headers = [(k, v) for k, v in upstream_resp.headers.items() if k.lower() not in hop]
    return Response(upstream_resp.content, status=upstream_resp.status_code, headers=out_headers)


def _mount_bundled_guest() -> None:
    """Serve the teammate app at /guest unless GUEST_UPSTREAM replaces it."""
    if (os.environ.get("GUEST_UPSTREAM") or "").strip():
        return
    guest_dir = Path(__file__).resolve().parent
    if not (guest_dir / "video_ask_app.py").is_file():
        return
    if str(guest_dir) not in sys.path:
        sys.path.insert(0, str(guest_dir))
    import video_ask_app as guest_mod
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/guest": guest_mod.app})


_mount_bundled_guest()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
