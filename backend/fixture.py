"""Local segments used when VSS_USE_FIXTURE=1. These are not archive footage."""

from __future__ import annotations

from typing import Any

from backend.models import Clip

CHUNKS: list[dict[str, Any]] = [
    {
        "segment_id": "seg-wh-1",
        "video_id": "vid-wh-1",
        "camera_id": "wh-cam-1",
        "location": "Warehouse aisle 2",
        "start": 12,
        "end": 20,
        "description": "A person is close to a moving forklift that is braking in the aisle.",
        "score": 0.91,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "forklift", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-wh-1",
        "previous": {"segment_id": "seg-wh-1-prev", "start": 4, "end": 12, "preview_source": "fixture://seg-wh-1-prev"},
        "next": {"segment_id": "seg-wh-1-next", "start": 20, "end": 28, "preview_source": "fixture://seg-wh-1-next"},
    },
    {
        "segment_id": "seg-wh-2",
        "video_id": "vid-wh-2",
        "camera_id": "wh-cam-1",
        "location": "Warehouse aisle 2",
        "start": 40,
        "end": 48,
        "description": "A person is near a forklift in the warehouse aisle.",
        "score": 0.74,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "forklift", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-wh-2",
        "previous": {"segment_id": "seg-wh-2-prev", "start": 32, "end": 40, "preview_source": "fixture://seg-wh-2-prev"},
        "next": {"segment_id": "seg-wh-2-next", "start": 48, "end": 56, "preview_source": "fixture://seg-wh-2-next"},
    },
    {
        "segment_id": "seg-road-1",
        "video_id": "vid-road-1",
        "camera_id": "road-cam-2",
        "location": "Street east",
        "start": 8,
        "end": 16,
        "description": "A pedestrian is close to a moving car that is swerving.",
        "score": 0.88,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "car", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-road-1",
        "previous": {"segment_id": "seg-road-1-prev", "start": 0, "end": 8, "preview_source": "fixture://seg-road-1-prev"},
        "next": {"segment_id": "seg-road-1-next", "start": 16, "end": 24, "preview_source": "fixture://seg-road-1-next"},
    },
    {
        "segment_id": "seg-road-2",
        "video_id": "vid-road-2",
        "camera_id": "road-cam-2",
        "location": "Street east",
        "start": 30,
        "end": 38,
        "description": "A person is near a moving vehicle on the street.",
        "score": 0.66,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "vehicle", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-road-2",
        "previous": {"segment_id": "seg-road-2-prev", "start": 22, "end": 30, "preview_source": "fixture://seg-road-2-prev"},
    },
    {
        "segment_id": "seg-yard-1",
        "video_id": "vid-yard-1",
        "camera_id": "yard-cam-3",
        "location": "Yard gate",
        "start": 5,
        "end": 14,
        "description": "A person is close to a moving vehicle in the yard.",
        "score": 0.8,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "vehicle", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-yard-1",
        "previous": {"segment_id": "seg-yard-1-prev", "start": 0, "end": 5, "preview_source": "fixture://seg-yard-1-prev"},
        "next": {"segment_id": "seg-yard-1-next", "start": 14, "end": 22, "preview_source": "fixture://seg-yard-1-next"},
    },
    {
        "segment_id": "seg-yard-2",
        "video_id": "vid-yard-2",
        "camera_id": "yard-cam-3",
        "location": "Yard gate",
        "start": 50,
        "end": 58,
        "description": "A vehicle is braking hard while a person stands in the lane.",
        "score": 0.86,
        "detections": [
            {"class": "person", "count": 1, "boxes": None},
            {"class": "vehicle", "count": 1, "boxes": None},
        ],
        "preview_source": "fixture://seg-yard-2",
        "previous": {"segment_id": "seg-yard-2-prev", "start": 42, "end": 50, "preview_source": "fixture://seg-yard-2-prev"},
        "next": {"segment_id": "seg-yard-2-next", "start": 58, "end": 66, "preview_source": "fixture://seg-yard-2-next"},
    },
    {
        "segment_id": "seg-road-3",
        "video_id": "vid-road-3",
        "camera_id": "road-cam-2",
        "location": "Street east",
        "start": 70,
        "end": 78,
        "description": "An empty road with traffic lights only.",
        "score": 0.33,
        "detections": [{"class": "traffic light", "count": 2, "boxes": None}],
        "preview_source": "fixture://seg-road-3",
    },
]

_BY_ID = {row["segment_id"]: row for row in CHUNKS}


def camera_ids() -> list[str]:
    """Return fixture camera ids in display order."""
    ordered: list[str] = []
    for row in CHUNKS:
        camera_id = str(row["camera_id"])
        if camera_id not in ordered:
            ordered.append(camera_id)
    return ordered


def chunks_for_camera(camera_id: str) -> list[dict[str, Any]]:
    """Return fixture search chunks for one camera."""
    return [row for row in CHUNKS if row["camera_id"] == camera_id]


def neighbours_for(segment_id: str) -> dict[str, Clip | None]:
    """Return the previous and next clips recorded on a fixture segment."""
    row = _BY_ID.get(segment_id) or {}
    return {"previous": _clip(row.get("previous")), "next": _clip(row.get("next"))}


def _clip(raw: dict[str, Any] | None) -> Clip | None:
    if not raw or not raw.get("preview_source"):
        return None
    from backend.clips import clip_url_for_source

    url = clip_url_for_source(str(raw["preview_source"]))
    if not url:
        return None
    return Clip(
        segment_id=str(raw.get("segment_id") or ""),
        start=float(raw.get("start") or 0),
        end=float(raw.get("end") or 0),
        clip_url=url,
    )
