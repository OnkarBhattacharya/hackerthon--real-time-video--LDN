#!/usr/bin/env python3
"""Idea Upscaler — boards + VO → shot list → cinematic matches from VSS archive."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
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


def _search_shot(
    shot: dict[str, Any],
    top_k: int = 5,
    *,
    tags: list[str] | None = None,
    exclude_sources: set[str] | None = None,
) -> list[dict[str, Any]]:
    query = re.sub(r"\s+", " ", (shot.get("search_query") or "")).replace("—", "-").replace("–", "-").strip()
    body: dict[str, Any] = {
        "query": query[:400],
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


VOICES = {
    "en-us+f3": "US female",
    "en-us+m3": "US male",
    "en-gb+f2": "UK female",
    "en-gb+m3": "UK male",
}


def _has_audio(path: Path) -> bool:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return bool(proc.stdout.strip())


def _render_same_length_cut(
    source_clips: list[dict[str, Any]],
    match_sources: list[str],
    transcript: str,
    voice: str | None = "en-us+f3",
    bed: float = 0.22,
    duration_sec: float | None = None,
) -> dict[str, Any]:
    """Concat archive matches and keep their audio. Speak the VO over a quieter bed."""
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in match_sources:
        if isinstance(item, str):
            item = {"source": item, "title": "", "filename": item.rsplit("/", 1)[-1]}
        src = item.get("source") or ""
        if not src or src in seen:
            continue
        seen.add(src)
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
        src = item["source"]
        start = round(i * slice_len, 2)
        end = round(target if i == len(picks) - 1 else (i + 1) * slice_len, 2)
        timeline.append({
            "title": item.get("title") or f"Shot {i + 1}",
            "filename": item.get("filename") or src.rsplit("/", 1)[-1],
            "start_sec": start,
            "end_sec": end,
        })
        raw = work / f"raw_{i}.mp4"
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
    if lines:
        step = target / len(lines)
        blocks = []
        for i, line in enumerate(lines):
            blocks.append(
                f"{i+1}\n{_srt_time(i * step)} --> {_srt_time(min(target, (i + 1) * step))}\n{line}\n"
            )
        (work / "vo.srt").write_text("\n".join(blocks), encoding="utf-8")
        vf += (
            ",subtitles=vo.srt:force_style="
            "'FontName=DejaVu Sans,FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2'"
        )

    spoken = voice in VOICES and bool(lines)
    if spoken:
        text = ". ".join(lines)
        wav = work / "vo.wav"
        proc = subprocess.run(
            ["espeak-ng", "-v", voice, "-s", "150", "-w", str(wav), text],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "espeak-ng failed")[-400:])
        bed_v = min(0.8, max(0.05, bed))
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(concat), "-i", str(wav),
            "-filter_complex",
            f"[0:a]volume={bed_v:.2f}[bed];[1:a]apad,volume=1.0[vo];[bed][vo]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "0:v", "-map", "[a]",
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
        "voice": voice if spoken else "off",
        "bed": bed,
        "timeline": timeline,
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
        voice = (payload.get("voice") or "en-us+f3").strip()
        if voice != "off" and voice not in VOICES:
            voice = "en-us+f3"
        try:
            bed = float(payload.get("bed") if payload.get("bed") is not None else 0.22)
        except (TypeError, ValueError):
            bed = 0.22
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

        splitter = "wandb"
        shots = _wandb_shots(logline, beats, transcript)
        if not shots:
            splitter = "heuristic"
            shots = _heuristic_shots(logline, beats, transcript)
        shots = shots[:4]

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
        match_sources: list[dict[str, Any]] = []
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
                match_sources.append({
                    "source": best["source"],
                    "title": shot.get("title") or "",
                    "filename": best.get("filename") or "",
                })

        rendered = None
        render_error = None
        if match_sources:
            try:
                rendered = _render_same_length_cut(
                    (source_upload or {}).get("clips") or [],
                    match_sources,
                    transcript,
                    None if voice == "off" else voice,
                    bed,
                    duration_sec,
                )
            except Exception as e:
                render_error = str(e)

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
  select { font: inherit; padding: 8px 10px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
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
  .tl { display: flex; width: 100%; height: 44px; margin-top: 10px; gap: 2px; }
  .tl button { flex: 1 1 0; min-width: 0; border: none; color: #f7f3ea; font: inherit; font-size: 0.72rem; padding: 4px; cursor: pointer; text-align: left; overflow: hidden; }
  .tl button small { display: block; opacity: 0.85; }
  .fb { margin-top: 12px; display: grid; gap: 8px; }
  .fb button.on { outline: 2px solid var(--ink); }
  .marks { display: flex; flex-direction: column; gap: 6px; }
  .shot-fb { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .shot-fb span { flex: 1 1 140px; font-size: 0.85rem; }
  .shot-fb button.on { outline: 2px solid var(--ink); background: #fff; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <p class="status"><a href="/app/">All apps</a></p>
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
        <div class="actions" style="margin-top:12px">
          <div>
            <label for="voice">Voice</label>
            <select id="voice">
              <option value="en-us+f3">US female</option>
              <option value="en-us+m3">US male</option>
              <option value="en-gb+f2">UK female</option>
              <option value="en-gb+m3">UK male</option>
              <option value="off">No voiceover</option>
            </select>
          </div>
          <div>
            <label for="bed">Footage audio under the voice</label>
            <select id="bed">
              <option value="0.22">Quiet</option>
              <option value="0.40">Medium</option>
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
        <ol class="progress hidden" id="progress"></ol>
        <div class="actions">
          <button id="go" type="button">Upscale idea</button>
          <button id="demo" class="ghost" type="button">Streets · NYC bike</button>
          <button id="shitty" class="ghost" type="button">Upscale shittyvideo</button>
          <span class="status" id="status"></span>
        </div>
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

async function runUpscale({ seed }) {
  const logline = $("logline").value.trim();
  const useSeed = seed || $("seed").checked;
  if (!logline && !useSeed) {
    $("status").textContent = "Logline is required, or use Upscale shittyvideo.";
    $("status").className = "status err";
    return;
  }
  const steps = useSeed
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
  if (run) runUpscale({ seed: false });
}

$("briefs").innerHTML = BRIEFS.slice(1).map((b, i) =>
  `<button type="button" class="ghost" data-brief="${i + 1}">${b.label}</button>`
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
        <button type="button" data-t="${row.start_sec}" style="background:${colors[i % colors.length]}">
          ${escapeHtml(row.title)}
          <small>${fmtTime(row.start_sec)}–${fmtTime(row.end_sec)}</small>
        </button>`).join("");
      tl.querySelectorAll("button").forEach(btn => {
        btn.onclick = () => { vid.currentTime = Number(btn.dataset.t) || 0; vid.play(); };
      });
    } else {
      tl.classList.add("hidden");
      tl.innerHTML = "";
    }
    $("cut-meta").textContent = `${reel.target_duration_sec}s · ${reel.pieces} shots` + (reel.vo ? " · voice over quieter footage audio" : " · footage audio only");
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
