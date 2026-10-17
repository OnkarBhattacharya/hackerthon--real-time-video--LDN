from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import ensure_video_layout, video_dir
from app.models import ModelClient


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MomentStore:
    """Per-video index persisted as data/<vid>/index.json."""

    def __init__(self, models: ModelClient) -> None:
        self.models = models
        self._lock = threading.Lock()

    def _index_path(self, vid: str) -> Path:
        return video_dir(vid) / "index.json"

    def load(self, vid: str) -> dict[str, Any]:
        path = self._index_path(vid)
        if not path.exists():
            return {
                "vid": vid,
                "moments": [],
                "detections": [],
                "meta": {},
            }
        with path.open(encoding="utf-8") as f:
            return json.load(f)

    def save(self, vid: str, index: dict[str, Any]) -> None:
        ensure_video_layout(vid)
        path = self._index_path(vid)
        with self._lock:
            path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    def add_moment(
        self,
        vid: str,
        *,
        chunk_id: str,
        start_sec: float,
        end_sec: float,
        caption: str,
        frame_path: str,
        embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        index = self.load(vid)
        moment_id = f"m_{uuid4().hex[:10]}"
        emb = embedding if embedding is not None else self.models.embed_text(caption)
        moment = {
            "moment_id": moment_id,
            "chunk_id": chunk_id,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "caption": caption,
            "frame_path": frame_path,
            "embedding": emb,
        }
        index["moments"].append(moment)
        self.save(vid, index)
        return moment

    def add_detections(
        self,
        vid: str,
        *,
        moment_id: str,
        frame_path: str,
        items: list[dict[str, Any]],
    ) -> None:
        index = self.load(vid)
        for item in items:
            index["detections"].append(
                {
                    "moment_id": moment_id,
                    "frame_path": frame_path,
                    **item,
                }
            )
        self.save(vid, index)

    def search_moments(
        self, vid: str, query: str, limit: int = 8
    ) -> list[dict[str, Any]]:
        index = self.load(vid)
        q_emb = self.models.embed_text(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in index.get("moments", []):
            score = _cosine(q_emb, m.get("embedding") or [])
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[dict[str, Any]] = []
        for score, m in scored[:limit]:
            out.append(
                {
                    "moment_id": m["moment_id"],
                    "score": round(score, 4),
                    "caption": m.get("caption"),
                    "start_sec": m.get("start_sec"),
                    "end_sec": m.get("end_sec"),
                    "frame_path": m.get("frame_path"),
                }
            )
        return out

    def query_detections(
        self,
        vid: str,
        label: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        index = self.load(vid)
        results = []
        for d in index.get("detections", []):
            if label and d.get("label") != label:
                continue
            if float(d.get("confidence", 0)) < min_confidence:
                continue
            results.append(d)
            if len(results) >= limit:
                break
        return results

    def get_moment(self, vid: str, moment_id: str) -> dict[str, Any] | None:
        index = self.load(vid)
        for m in index.get("moments", []):
            if m.get("moment_id") == moment_id:
                return m
        return None
