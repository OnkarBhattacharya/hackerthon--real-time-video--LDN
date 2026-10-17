"""Domain models for Stage 1 candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DetectionBox:
    """Optional axis-aligned box in frame coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float | None = None
    frame_index: int | None = None


@dataclass
class Detection:
    """Aggregated detection for one object class."""

    class_name: str
    count: int
    boxes: list[DetectionBox] = field(default_factory=list)


@dataclass
class Candidate:
    """Normalised search hit used by later ranking / evidence stages."""

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
    segment_number: int | None = None
    total_segments: int | None = None
    filename: str | None = None
    capture_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "video_id": self.video_id,
            "camera_id": self.camera_id,
            "location": self.location,
            "start": self.start,
            "end": self.end,
            "description": self.description,
            "detections": [
                {
                    "class": d.class_name,
                    "count": d.count,
                    "boxes": [
                        {
                            "x1": b.x1,
                            "y1": b.y1,
                            "x2": b.x2,
                            "y2": b.y2,
                            "conf": b.conf,
                            "frame_index": b.frame_index,
                        }
                        for b in d.boxes
                    ],
                }
                for d in self.detections
            ],
            "search_score": self.search_score,
            "clip_url": self.clip_url,
            "segment_number": self.segment_number,
            "total_segments": self.total_segments,
            "filename": self.filename,
            "capture_type": self.capture_type,
        }


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class RankedHit:
    """Candidate plus LLM (or fallback) severity ranking fields."""

    candidate: Candidate
    severity: int
    category: str
    rationale: str
    people_visible: bool
    faces_or_plates_possibly_visible: bool
    recommended_action: str
    confidence: str
    ranking_source: str = "llm"  # llm | fallback | repair

    @property
    def segment_id(self) -> str:
        return self.candidate.segment_id

    @property
    def search_score(self) -> float:
        return self.candidate.search_score

    def to_dict(self) -> dict[str, Any]:
        base = self.candidate.to_dict()
        base.update(
            {
                "severity": self.severity,
                "category": self.category,
                "rationale": self.rationale,
                "people_visible": self.people_visible,
                "faces_or_plates_possibly_visible": self.faces_or_plates_possibly_visible,
                "recommended_action": self.recommended_action,
                "confidence": self.confidence,
                "ranking_source": self.ranking_source,
            }
        )
        return base

