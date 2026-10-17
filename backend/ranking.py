"""Severity ranking for near-miss candidates via W&B LLM (with fallback)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from backend.config import Settings, load_settings
from backend.llm import LlmClient, LlmError
from backend.models import CONFIDENCE_RANK, Candidate, RankedHit

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a video safety analyst. Use ONLY the provided description and detections. Never invent facts.
Return ONE JSON object with exactly these keys:
segment_id (string), severity (integer 1-5), category (one of: highway, driving, street, warehouse, indoor, other),
rationale (1-2 sentences citing evidence), people_visible (boolean), faces_or_plates_possibly_visible (boolean),
recommended_action (short string), confidence (one of: low, medium, high).
Severity rubric:
1 = no real interaction (person and vehicle far apart, or no person).
2 = same scene, no close approach.
3 = close approach (person within about one vehicle length of a moving vehicle) with no evasive action.
4 = close approach with evasive action (braking, swerving, stepping back), or a vulnerable road user in the vehicle's path.
5 = apparent contact or an extreme near-miss.
In a warehouse, treat a forklift as the moving vehicle and a person in the aisle or walkway as the pedestrian.
If evidence is weak, lower severity and confidence."""

CATEGORIES = frozenset({"highway", "driving", "street", "warehouse", "indoor", "other"})
CONFIDENCES = frozenset({"low", "medium", "high"})
KEYWORD_SEVERITY = (
    (5, re.compile(r"\b(collision|impact|hit|struck|crash|contact)\b", re.I)),
    (4, re.compile(r"\b(brak(?:e|ing)|swerv(?:e|ing)|near[- ]?miss|steps? back|evasive)\b", re.I)),
    (3, re.compile(r"\b(close|near|crossing|crosswalk|in (?:the )?path|approaching)\b", re.I)),
)


def rank_candidates(
    candidates: list[Candidate],
    *,
    settings: Settings | None = None,
    llm: LlmClient | None = None,
    keep_min_severity: int = 3,
    min_keep: int = 6,
) -> list[RankedHit]:
    """Score candidates, filter to actionable hits, and sort."""
    cfg = settings or load_settings()
    client = llm or LlmClient(cfg)
    if not candidates:
        return []

    ranked: list[RankedHit] = []
    workers = max(1, min(cfg.ranking_concurrency, len(candidates)))
    log.info("ranking.start n=%s concurrency=%s llm=%s", len(candidates), workers, client.available)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_rank_one, cand, client): cand.segment_id for cand in candidates
        }
        for fut in as_completed(futures):
            ranked.append(fut.result())

    kept = [h for h in ranked if h.severity >= keep_min_severity]
    if len(kept) < min_keep:
        ranked_sorted = _sort_hits(ranked)
        kept = ranked_sorted[: max(min_keep, len(ranked_sorted))]
        log.info(
            "ranking.widen kept=%s (below min_keep=%s at severity>=%s)",
            len(kept),
            min_keep,
            keep_min_severity,
        )
    else:
        kept = _sort_hits(kept)
    log.info("ranking.done kept=%s", len(kept))
    return kept


def _rank_one(candidate: Candidate, llm: LlmClient) -> RankedHit:
    if not llm.available:
        hit = _fallback_rank(candidate)
        hit.ranking_source = "fallback"
        return hit
    try:
        raw = llm.chat_json(
            system=SYSTEM_PROMPT,
            user=_user_prompt(candidate),
            max_tokens=800,
            temperature=0.0,
        )
        return _hit_from_json(candidate, raw, source="llm")
    except (LlmError, ValueError, KeyError, TypeError) as exc:
        log.warning(
            "ranking.fallback segment=%s reason=%s",
            candidate.segment_id[-48:],
            type(exc).__name__,
        )
        hit = _fallback_rank(candidate)
        hit.ranking_source = "fallback"
        return hit


def _user_prompt(c: Candidate) -> str:
    dets = [{"class": d.class_name, "count": d.count} for d in c.detections]
    return (
        f"Score this segment.\n"
        f'segment_id: "{c.segment_id}"\n'
        f'camera_id: "{c.camera_id}"\n'
        f'location: "{c.location}"\n'
        f'capture_type: "{c.capture_type or ""}"\n'
        f"search_score: {c.search_score:.4f}\n"
        f'description: "{c.description}"\n'
        f"detections: {dets}\n"
    )


def _hit_from_json(candidate: Candidate, raw: dict[str, Any], *, source: str) -> RankedHit:
    severity = int(raw.get("severity", 1))
    severity = max(1, min(5, severity))
    category = str(raw.get("category") or "other").lower().strip()
    if category not in CATEGORIES:
        category = _guess_category(candidate)
    confidence = str(raw.get("confidence") or "low").lower().strip()
    if confidence not in CONFIDENCES:
        confidence = "low"
    return RankedHit(
        candidate=candidate,
        severity=severity,
        category=category,
        rationale=str(raw.get("rationale") or "").strip() or "Insufficient evidence.",
        people_visible=bool(raw.get("people_visible")),
        faces_or_plates_possibly_visible=bool(raw.get("faces_or_plates_possibly_visible")),
        recommended_action=str(raw.get("recommended_action") or "Review clip manually.").strip(),
        confidence=confidence,
        ranking_source=source,
    )


def _fallback_rank(candidate: Candidate) -> RankedHit:
    """Rule-based score when LLM is unavailable or fails."""
    classes = {d.class_name.lower() for d in candidate.detections}
    person = any(c in classes for c in ("person", "pedestrian", "rider", "cyclist"))
    vehicle = any(
        c in classes
        for c in ("car", "truck", "bus", "motorcycle", "bicycle", "forklift", "vehicle")
    )
    text = candidate.description or ""
    severity = 1
    if person and vehicle:
        severity = 2
    for sev, pattern in KEYWORD_SEVERITY:
        if pattern.search(text):
            severity = max(severity, sev if person or vehicle else min(sev, 2))
            break
    if candidate.search_score >= 0.4 and person and vehicle:
        severity = max(severity, 3)
    rationale = (
        f"Fallback rank from search_score={candidate.search_score:.3f}, "
        f"person={person}, vehicle={vehicle}, keywords in description."
    )
    return RankedHit(
        candidate=candidate,
        severity=severity,
        category=_guess_category(candidate),
        rationale=rationale,
        people_visible=person,
        faces_or_plates_possibly_visible=person,
        recommended_action="Manual review recommended (rule-based ranking).",
        confidence="low",
        ranking_source="fallback",
    )


def _guess_category(candidate: Candidate) -> str:
    loc = (candidate.location or "").lower()
    cap = (candidate.capture_type or "").lower()
    if "warehouse" in loc or cap == "warehouse":
        return "warehouse"
    if "indoor" in loc:
        return "indoor"
    if any(x in loc for x in ("highway", "i24")) or cap == "traffic":
        return "highway"
    if any(x in loc for x in ("street", "york", "francisco", "nashville", "toronto", "neighborhood")):
        return "street"
    return "other"


def _sort_hits(hits: list[RankedHit]) -> list[RankedHit]:
    return sorted(
        hits,
        key=lambda h: (
            h.severity,
            CONFIDENCE_RANK.get(h.confidence, 0),
            h.search_score,
        ),
        reverse=True,
    )
