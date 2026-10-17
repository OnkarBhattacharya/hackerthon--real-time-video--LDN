"""Attach neighbour clips and privacy flags, and persist the last run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import CROP_AVAILABLE, settings
from backend.models import PRIVACY_NOTE, Clip, EvidenceHit, RankedHit
from backend.vss import get_client


def assemble_evidence(hits: list[RankedHit]) -> list[EvidenceHit]:
    """Attach neighbour clips, an optional crop, and privacy flags."""
    client = get_client()
    assembled: list[EvidenceHit] = []
    for hit in hits:
        candidate = hit.candidate
        neighbours = client.neighbouring_segments(
            candidate.segment_id,
            candidate.video_id,
            candidate.start,
            candidate.end,
        )
        missing: list[str] = []
        clips: dict[str, dict[str, Any] | None] = {}
        event = _event_clip(hit)
        clip_error = None
        if event is None:
            clips["event"] = None
            clip_error = "No playable event clip was returned for this segment."
        else:
            clips["event"] = event.to_dict()
        for role in ("previous", "next"):
            clip = neighbours.get(role)
            if clip is None or not clip.clip_url:
                missing.append(role)
                clips[role] = None
            else:
                clips[role] = clip.to_dict()
        assembled.append(
            EvidenceHit(
                ranked=hit,
                clips=clips,
                missing_neighbours=missing,
                privacy_flags=_privacy_flags(hit),
                privacy_note=PRIVACY_NOTE,
                crop_url=None,
                crop_available=CROP_AVAILABLE,
                clip_error=clip_error,
            )
        )
    return assembled


def save_snapshot(
    hits: list[dict[str, Any]],
    queries: list[str],
    cameras: list[str],
    path: Path | None = None,
) -> None:
    """Write the latest successful run so the API can reload it."""
    target = path or settings.snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "queries": queries,
        "cameras": cameras,
        "hits": hits,
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    """Return the last run, or None when no snapshot exists."""
    target = path or settings.snapshot_path()
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _event_clip(hit: RankedHit) -> Clip | None:
    candidate = hit.candidate
    if not candidate.clip_url:
        return None
    return Clip(candidate.segment_id, candidate.start, candidate.end, candidate.clip_url)


def _privacy_flags(hit: RankedHit) -> list[str]:
    labels = {item.label.lower() for item in hit.candidate.detections}
    flags: list[str] = []
    if hit.people_visible or "person" in labels or "pedestrian" in labels:
        flags.append("people visible")
    if hit.faces_or_plates_possibly_visible:
        flags.append("faces or licence plates possibly visible")
    return flags
