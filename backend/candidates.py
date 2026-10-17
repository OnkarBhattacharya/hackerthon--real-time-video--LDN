"""Search every camera and merge the hits into one candidate list."""

from __future__ import annotations

import logging
from collections.abc import Callable

from backend.config import settings
from backend.models import Candidate
from backend.vss import VssError, get_client

logger = logging.getLogger(__name__)

DEFAULT_QUERIES = [
    "person close to a moving vehicle",
    "pedestrian crossing in front of a vehicle",
    "vehicle braking hard",
    "forklift near a person",
    "person walking in a vehicle lane",
]


def find_candidates(
    queries: list[str],
    cameras: list[str],
    per_query_limit: int,
    on_step: Callable[[str], None] | None = None,
) -> list[Candidate]:
    """Search each camera for each query and return the best normalised hits."""
    step = on_step or (lambda _message: None)
    client = get_client()
    client.clear_cache()
    indexed = set(client.list_cameras())
    blocked = settings.cameras_not_indexed()
    found: list[Candidate] = []
    searched = 0
    failures = 0
    for camera_id in _unique(cameras):
        if camera_id in blocked or camera_id not in indexed:
            step(f"Skipping camera {camera_id}; it is not indexed")
            continue
        step(f"Searching camera {camera_id}")
        searched += 1
        camera_hits: list[Candidate] = []
        failed = False
        for query in queries:
            try:
                camera_hits.extend(client.search(query, camera_id, per_query_limit))
            except VssError:
                logger.info("search failed camera=%s", camera_id)
                failed = True
                break
        if failed and not camera_hits:
            failures += 1
            step(f"Search failed for camera {camera_id}")
            continue
        found.extend(camera_hits)
    if searched > 0 and failures == searched:
        raise VssError(
            "search",
            None,
            "Check login, then the dashboard, then valid metadata values.",
        )
    return merge_candidates(found)


def merge_candidates(
    items: list[Candidate],
    *,
    per_camera_limit: int = 12,
    overall_limit: int = 40,
) -> list[Candidate]:
    """De-duplicate by segment id, cap each camera, then keep the best overall."""
    best: dict[str, Candidate] = {}
    for item in items:
        current = best.get(item.segment_id)
        if current is None or item.search_score > current.search_score:
            best[item.segment_id] = item
    grouped: dict[str, list[Candidate]] = {}
    for item in best.values():
        grouped.setdefault(item.camera_id, []).append(item)
    capped: list[Candidate] = []
    for group in grouped.values():
        group.sort(key=lambda item: item.search_score, reverse=True)
        capped.extend(group[:per_camera_limit])
    capped.sort(key=lambda item: item.search_score, reverse=True)
    return capped[:overall_limit]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
