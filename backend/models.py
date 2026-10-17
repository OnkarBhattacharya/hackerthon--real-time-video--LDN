"""Shared investigation records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PRIVACY_NOTE = "Review before sharing. Possible personal data (GDPR)."


@dataclass
class Detection:
    """One detected class on a segment."""

    label: str
    count: int
    boxes: list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the API shape. `class` is the wire name."""
        return {"class": self.label, "count": self.count, "boxes": self.boxes}


@dataclass
class Candidate:
    """One normalised search hit."""

    segment_id: str
    video_id: str
    camera_id: str
    location: str
    start: float
    end: float
    description: str
    detections: list[Detection]
    search_score: float
    clip_url: str


@dataclass
class Clip:
    """A playable neighbour or event clip."""

    segment_id: str
    start: float
    end: float
    clip_url: str

    def to_dict(self) -> dict[str, Any]:
        """Return the clip fields the UI plays."""
        return {
            "segment_id": self.segment_id,
            "start": self.start,
            "end": self.end,
            "clip_url": self.clip_url,
        }


@dataclass
class RankedHit:
    """A candidate plus a severity judgement."""

    candidate: Candidate
    severity: int
    category: str
    rationale: str
    people_visible: bool
    faces_or_plates_possibly_visible: bool
    recommended_action: str
    confidence: str
    rule_based: bool = False


@dataclass
class EvidenceHit:
    """A ranked hit with clips and privacy flags."""

    ranked: RankedHit
    clips: dict[str, dict[str, Any] | None]
    missing_neighbours: list[str] = field(default_factory=list)
    privacy_flags: list[str] = field(default_factory=list)
    privacy_note: str = PRIVACY_NOTE
    crop_url: str | None = None
    crop_available: bool = False
    clip_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the flat JSON object stored in jobs and the snapshot."""
        candidate = self.ranked.candidate
        payload: dict[str, Any] = {
            "segment_id": candidate.segment_id,
            "video_id": candidate.video_id,
            "camera_id": candidate.camera_id,
            "location": candidate.location,
            "start": candidate.start,
            "end": candidate.end,
            "description": candidate.description,
            "detections": [item.to_dict() for item in candidate.detections],
            "search_score": candidate.search_score,
            "clip_url": candidate.clip_url,
            "severity": self.ranked.severity,
            "category": self.ranked.category,
            "rationale": self.ranked.rationale,
            "people_visible": self.ranked.people_visible,
            "faces_or_plates_possibly_visible": self.ranked.faces_or_plates_possibly_visible,
            "recommended_action": self.ranked.recommended_action,
            "confidence": self.ranked.confidence,
            "rule_based": self.ranked.rule_based,
            "clips": self.clips,
            "missing_neighbours": list(self.missing_neighbours),
            "privacy_flags": list(self.privacy_flags),
            "privacy_note": self.privacy_note,
            "crop_url": self.crop_url,
            "crop_available": self.crop_available,
        }
        if self.clip_error:
            payload["clip_error"] = self.clip_error
        return payload


def evidence_from_dict(data: dict[str, Any]) -> EvidenceHit:
    """Rebuild an evidence hit from a snapshot or job payload."""
    detections = [
        Detection(
            label=str(item.get("class") or ""),
            count=int(item.get("count") or 0),
            boxes=item.get("boxes"),
        )
        for item in data.get("detections") or []
        if isinstance(item, dict)
    ]
    candidate = Candidate(
        segment_id=str(data.get("segment_id") or ""),
        video_id=str(data.get("video_id") or ""),
        camera_id=str(data.get("camera_id") or ""),
        location=str(data.get("location") or ""),
        start=float(data.get("start") or 0),
        end=float(data.get("end") or 0),
        description=str(data.get("description") or ""),
        detections=detections,
        search_score=float(data.get("search_score") or 0),
        clip_url=str(data.get("clip_url") or ""),
    )
    ranked = RankedHit(
        candidate=candidate,
        severity=int(data.get("severity") or 1),
        category=str(data.get("category") or "other"),
        rationale=str(data.get("rationale") or ""),
        people_visible=bool(data.get("people_visible")),
        faces_or_plates_possibly_visible=bool(data.get("faces_or_plates_possibly_visible")),
        recommended_action=str(data.get("recommended_action") or ""),
        confidence=str(data.get("confidence") or "low"),
        rule_based=bool(data.get("rule_based")),
    )
    return EvidenceHit(
        ranked=ranked,
        clips=dict(data.get("clips") or {}),
        missing_neighbours=list(data.get("missing_neighbours") or []),
        privacy_flags=list(data.get("privacy_flags") or []),
        privacy_note=str(data.get("privacy_note") or PRIVACY_NOTE),
        crop_url=data.get("crop_url"),
        crop_available=bool(data.get("crop_available")),
        clip_error=data.get("clip_error"),
    )
