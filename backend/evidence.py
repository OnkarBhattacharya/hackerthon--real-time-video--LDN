"""Assemble neighbour-clip triptychs and privacy flags for ranked hits."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.models import RankedHit
from backend.vss import VssClient, VssRequestError

log = logging.getLogger(__name__)

GDPR_NOTE = "Review before sharing. Possible personal data (GDPR)."
CROP_REASON = (
    "Detections include bounding boxes only; no downloadable frame image."
)
DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "last_run.json"

_PERSON_HINTS = ("person", "pedestrian", "people", "human", "worker", "cyclist", "rider")
_PLATE_HINTS = ("plate", "face")

_last_run: dict[str, Any] | None = None


@dataclass
class TriptychSlot:
    """One clip in the before / event / after row."""

    role: str
    present: bool
    clip_url: str | None = None
    source: str | None = None
    segment_number: int | None = None
    start: float | None = None
    end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "present": self.present}
        if not self.present:
            return out
        out.update(
            {
                "clip_url": self.clip_url,
                "source": self.source,
                "segment_number": self.segment_number,
                "start": self.start,
                "end": self.end,
            }
        )
        return out


@dataclass
class EvidenceBundle:
    """Ranked hit plus triptych, privacy flags, and crop decision."""

    hit: RankedHit
    triptych: dict[str, TriptychSlot]
    neighbour_flags: list[str]
    privacy_flags: list[str]
    crop_available: bool = False
    crop_omitted: bool = True
    crop_reason: str = CROP_REASON

    def to_dict(self) -> dict[str, Any]:
        data = self.hit.to_dict()
        data.update(
            {
                "triptych": {role: slot.to_dict() for role, slot in self.triptych.items()},
                "neighbour_flags": list(self.neighbour_flags),
                "privacy_flags": list(self.privacy_flags),
                "crop": None,
                "crop_available": self.crop_available,
                "crop_omitted": self.crop_omitted,
                "crop_reason": self.crop_reason,
            }
        )
        return data


def build_privacy_flags(hit: RankedHit) -> list[str]:
    """Flags from LLM booleans and detection class names, plus the GDPR note."""
    classes = [d.class_name.lower() for d in hit.candidate.detections]
    flags: list[str] = []
    people = hit.people_visible or _class_matches(classes, _PERSON_HINTS)
    plates = hit.faces_or_plates_possibly_visible or _class_matches(classes, _PLATE_HINTS)
    if people:
        flags.append("people visible")
    if plates:
        flags.append("faces or licence plates possibly visible")
    flags.append(GDPR_NOTE)
    return flags


def assemble_evidence(
    hits: list[RankedHit],
    *,
    client: VssClient | None = None,
    snapshot_path: Path | None = None,
) -> list[EvidenceBundle]:
    """Build a triptych per hit, store it in memory, and write the JSON snapshot."""
    vss = client or VssClient()
    bundles: list[EvidenceBundle] = []
    for hit in hits:
        bundle = _bundle_one(hit, vss)
        bundles.append(bundle)
        log.info(
            "evidence.hit camera=%s severity=%s flags=%s prev=%s event=%s next=%s",
            hit.candidate.camera_id,
            hit.severity,
            ",".join(bundle.neighbour_flags) or "-",
            bundle.triptych["previous"].present,
            bundle.triptych["event"].present,
            bundle.triptych["next"].present,
        )
    store_run(bundles, path=snapshot_path)
    return bundles


def store_run(bundles: list[EvidenceBundle], *, path: Path | None = None) -> Path:
    """Remember this run in memory and write data/last_run.json."""
    global _last_run
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "crop_available": False,
        "crop_reason": CROP_REASON,
        "hit_count": len(bundles),
        "hits": [b.to_dict() for b in bundles],
    }
    _last_run = payload
    dest = path or DEFAULT_SNAPSHOT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("evidence.snapshot hits=%s path=%s", len(bundles), dest)
    return dest


def get_last_run() -> dict[str, Any] | None:
    """Return the in-memory snapshot from this process, if any."""
    return _last_run


def load_last_run(path: Path | None = None) -> dict[str, Any] | None:
    """Load a snapshot from disk into memory. Returns None if the file is missing."""
    global _last_run
    dest = path or DEFAULT_SNAPSHOT
    if not dest.is_file():
        return None
    _last_run = json.loads(dest.read_text(encoding="utf-8"))
    log.info("evidence.loaded hits=%s path=%s", _last_run.get("hit_count"), dest)
    return _last_run


def _bundle_one(hit: RankedHit, client: VssClient) -> EvidenceBundle:
    neighbours = _neighbours_or_empty(hit, client)
    previous = _slot_from_row("previous", neighbours.get("previous"), client)
    event = _slot_from_row("event", neighbours.get("current"), client)
    if not event.present:
        event = TriptychSlot(
            role="event",
            present=True,
            clip_url=client.clip_url(hit.candidate.segment_id),
            source=hit.candidate.segment_id,
            segment_number=hit.candidate.segment_number,
            start=hit.candidate.start,
            end=hit.candidate.end,
        )
    nxt = _slot_from_row("next", neighbours.get("next"), client)
    flags: list[str] = []
    if not previous.present:
        flags.append("missing_previous")
    if not nxt.present:
        flags.append("missing_next")
    return EvidenceBundle(
        hit=hit,
        triptych={"previous": previous, "event": event, "next": nxt},
        neighbour_flags=flags,
        privacy_flags=build_privacy_flags(hit),
    )


def _neighbours_or_empty(hit: RankedHit, client: VssClient) -> dict[str, Any]:
    video_id = hit.candidate.video_id
    number = hit.candidate.segment_number
    empty: dict[str, Any] = {"previous": None, "current": None, "next": None}
    if not video_id or number is None:
        log.warning(
            "evidence.no_segment_index camera=%s",
            hit.candidate.camera_id,
        )
        return empty
    try:
        return client.neighbouring_segments(video_id, int(number))
    except VssRequestError:
        log.warning(
            "evidence.neighbours_failed camera=%s",
            hit.candidate.camera_id,
        )
        return empty


def _slot_from_row(
    role: str,
    row: dict[str, Any] | None,
    client: VssClient,
) -> TriptychSlot:
    source = str((row or {}).get("source") or "")
    if not row or not source:
        return TriptychSlot(role=role, present=False)
    number = row.get("segment_number")
    return TriptychSlot(
        role=role,
        present=True,
        clip_url=client.clip_url(source),
        source=source,
        segment_number=int(number) if number is not None else None,
        start=float(row.get("segment_start_sec") or 0.0),
        end=float(row.get("segment_end_sec") or 0.0),
    )


def _class_matches(classes: list[str], hints: tuple[str, ...]) -> bool:
    return any(hint in name for name in classes for hint in hints)
