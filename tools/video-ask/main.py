"""LDN Video Ask — thin app on the team's VSS retrieval APIs."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, jsonify, request, send_from_directory

import vss_client


def _stream_path(source: str | None) -> str | None:
    if not source:
        return None
    # Relative path so the UI can prefix /app when served behind Ingress rewrite.
    return f"api/stream?source={quote(source, safe='')}"

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "video-ask"})


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/app.js")
def app_js():
    return send_from_directory(HERE, "app.js")


@app.get("/style.css")
def style_css():
    return send_from_directory(HERE, "style.css")


@app.post("/api/ask")
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or data.get("query") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    top_k = int(data.get("top_k") or 10)
    min_similarity = float(data.get("min_similarity") or 0.25)
    time_filter = (data.get("time_filter") or "all").strip() or "all"
    try:
        raw = vss_client.search_and_answer(
            question,
            top_k=top_k,
            min_similarity=min_similarity,
            time_filter=time_filter,
            metadata_filters=data.get("metadata_filters"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    evidence = raw.get("evidence") or {}
    chunks = evidence.get("chunks") or []
    clips = []
    for c in chunks:
        preview = c.get("preview_source")
        clips.append(
            {
                "filename": c.get("filename"),
                "original_video": c.get("original_video"),
                "preview_source": preview,
                "similarity_score": c.get("similarity_score"),
                "start_sec": c.get("best_match_start_sec"),
                "end_sec": c.get("best_match_end_sec"),
                "matched_segment_count": c.get("matched_segment_count"),
                "stream_path": _stream_path(preview),
            }
        )

    synthesis = evidence.get("llm_synthesis") or {}
    return jsonify(
        {
            "answer": raw.get("answer") or synthesis.get("response") or "",
            "tool_used": raw.get("tool_used"),
            "clips": clips,
            "query": evidence.get("query") or question,
            "segment_hit_count": evidence.get("segment_hit_count"),
        }
    )


@app.get("/api/suggestions")
def api_suggestions():
    try:
        raw = vss_client.suggestions()
    except Exception as exc:
        return jsonify({"error": str(exc), "search_prompts": [], "key_events": []}), 502
    prompts = raw.get("search_prompts") or []
    events = raw.get("key_events") or []
    # Cap events for UI payload size
    lean_events = [
        {
            "label": e.get("label"),
            "query_text": e.get("query_text"),
            "filename": e.get("filename"),
            "segment_start_sec": e.get("segment_start_sec"),
            "segment_end_sec": e.get("segment_end_sec"),
        }
        for e in events[:12]
    ]
    return jsonify({"search_prompts": prompts[:12], "key_events": lean_events})


@app.get("/api/explore")
def api_explore():
    limit = int(request.args.get("limit") or 12)
    offset = int(request.args.get("offset") or 0)
    try:
        raw = vss_client.explore(limit=limit, offset=offset)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    chunks = []
    for c in raw.get("chunks") or []:
        preview = c.get("preview_source")
        chunks.append(
            {
                "filename": c.get("filename"),
                "location": c.get("location"),
                "camera_id": c.get("camera_id"),
                "capture_type": c.get("capture_type"),
                "reasoning_content": (c.get("reasoning_content") or "")[:400],
                "start_sec": c.get("best_match_start_sec"),
                "end_sec": c.get("best_match_end_sec"),
                "preview_source": preview,
                "stream_path": _stream_path(preview),
            }
        )
    return jsonify(
        {
            "total": raw.get("total"),
            "locations": raw.get("locations") or [],
            "chunks": chunks,
        }
    )


@app.get("/api/stream")
def api_stream():
    source = request.args.get("source") or ""
    if not source.startswith("s3://"):
        return jsonify({"error": "source must be an s3:// URI"}), 400
    try:
        upstream = vss_client.proxy_stream(source, request.headers.get("Range"))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    if upstream.status_code >= 400:
        return Response(upstream.content, status=upstream.status_code)

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
    }
    for h in ("Content-Length", "Content-Range"):
        if upstream.headers.get(h):
            headers[h] = upstream.headers[h]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(generate(), status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8080)
    app.run(host="0.0.0.0", port=port, threaded=True)
