"""Candidate discovery across cameras and near-miss queries."""

from __future__ import annotations

import logging
from collections import defaultdict

from backend.models import Candidate
from backend.vss import VssClient

log = logging.getLogger(__name__)

DEFAULT_QUERIES: list[str] = [
    "person close to a moving vehicle",
    "pedestrian crossing in front of a vehicle",
    "vehicle braking hard",
    "forklift near a person",
    "person walking in a vehicle lane",
]

MAX_PER_CAMERA = 12
MAX_OVERALL = 40


def find_candidates(
    queries: list[str] | None = None,
    cameras: list[str] | None = None,
    per_query_limit: int = 8,
    *,
    client: VssClient | None = None,
    enrich_boxes: bool = False,
) -> list[Candidate]:
    """Search each query×camera, de-dupe by segment, cap per camera and overall.

    Returns candidates sorted by search_score descending.
    """
    vss = client or VssClient()
    query_list = list(queries or DEFAULT_QUERIES)
    camera_list = list(cameras) if cameras is not None else vss.list_camera_ids()
    if not camera_list:
        log.warning("find_candidates: no cameras available")
        return []

    best: dict[str, Candidate] = {}
    for query in query_list:
        for camera_id in camera_list:
            try:
                rows = vss.search(
                    query,
                    top_k=per_query_limit,
                    camera_id=camera_id,
                )
            except Exception as exc:
                log.warning(
                    "find_candidates: search failed camera=%s query=%r err=%s",
                    camera_id,
                    query[:60],
                    type(exc).__name__,
                )
                continue
            log.info(
                "find_candidates: camera=%s query=%r hits=%s",
                camera_id,
                query[:60],
                len(rows),
            )
            for row in rows:
                try:
                    cand = vss.row_to_candidate(row, enrich_boxes=enrich_boxes)
                except ValueError:
                    continue
                existing = best.get(cand.segment_id)
                if existing is None or cand.search_score > existing.search_score:
                    best[cand.segment_id] = cand

    by_camera: dict[str, list[Candidate]] = defaultdict(list)
    for cand in best.values():
        by_camera[cand.camera_id].append(cand)

    capped: list[Candidate] = []
    for camera_id, items in by_camera.items():
        items.sort(key=lambda c: c.search_score, reverse=True)
        kept = items[:MAX_PER_CAMERA]
        capped.extend(kept)
        log.info(
            "find_candidates: camera=%s unique=%s kept=%s",
            camera_id,
            len(items),
            len(kept),
        )

    capped.sort(key=lambda c: c.search_score, reverse=True)
    return capped[:MAX_OVERALL]
