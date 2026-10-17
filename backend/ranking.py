"""Severity ranking. Uses the inference model when it is configured, otherwise rules."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from backend.llm import LlmError, llm
from backend.models import Candidate, RankedHit

logger = logging.getLogger(__name__)

CATEGORIES = {"highway", "driving", "street", "warehouse", "indoor", "other"}
CONFIDENCE = {"low", "medium", "high"}
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
PERSON_LABELS = {"person", "pedestrian", "people", "human"}
VEHICLE_LABELS = {"car", "truck", "bus", "vehicle", "forklift", "motorcycle", "van", "bike", "bicycle"}
RUBRIC = """
Score 1 to 5 using only the description and detections given to you.
1 = no real interaction (person and vehicle far apart, or no person).
2 = same scene, no close approach.
3 = close approach (a person within about one vehicle length of a moving vehicle) with no evasive action.
4 = close approach with evasive action (braking, swerving, stepping back), or a vulnerable road user in the vehicle's path.
5 = apparent contact or an extreme near-miss.
In a warehouse, treat a forklift as the moving vehicle and a person in the aisle or walkway as the pedestrian.
If the evidence is weak, lower the score and say so. Never invent details.
""".strip()

SYSTEM = (
    "You score near-miss video segments. Reply with one JSON object only.\n"
    + RUBRIC
    + '\nRequired keys: segment_id, severity, category, rationale, people_visible, '
    "faces_or_plates_possibly_visible, recommended_action, confidence.\n"
    "category is one of highway, driving, street, warehouse, indoor, other.\n"
    "confidence is one of low, medium, high.\n"
    "severity is an integer from 1 to 5.\n"
    "people_visible and faces_or_plates_possibly_visible are booleans.\n"
    "rationale is one or two sentences citing the evidence."
)

Complete = Callable[[str], str]


class ValidationError(Exception):
    """A model response did not match the ranking schema."""


def rank_candidates(
    candidates: list[Candidate],
    complete: Complete | None = None,
) -> list[RankedHit]:
    """Score candidates and return those kept, sorted for the UI."""
    completer = complete
    if completer is None and llm.configured():
        completer = _model_complete
    if completer is None:
        logger.info("ranking mode=rules count=%s", len(candidates))
        return select_hits([rule_rank(candidate) for candidate in candidates])
    logger.info("ranking mode=model count=%s", len(candidates))
    ranked: list[RankedHit | None] = [None] * len(candidates)
    workers = min(4, max(1, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_rank_one, candidate, completer): index
            for index, candidate in enumerate(candidates)
        }
        for future in as_completed(futures):
            ranked[futures[future]] = future.result()
    return select_hits([hit for hit in ranked if hit is not None])


def select_hits(ranked: list[RankedHit]) -> list[RankedHit]:
    """Keep severity of at least 3, or the top 6, then sort."""
    kept = [hit for hit in ranked if hit.severity >= 3]
    if len(kept) < 6:
        pool = sorted(ranked, key=_fill_key)
        kept = pool[: min(6, len(pool))]
    kept.sort(key=_sort_key)
    return kept


def rule_rank(candidate: Candidate) -> RankedHit:
    """Score one candidate from detections, description words, and leave search_score for sorting."""
    text = candidate.description.lower()
    persons = _count(candidate, PERSON_LABELS)
    vehicles = _count(candidate, VEHICLE_LABELS)
    person_seen = persons > 0 or _mentions_person(text)
    vehicle_seen = vehicles > 0 or bool(
        re.search(r"\b(vehicle|car|forklift|truck|bus|van)\b", text)
    )
    severity = 1
    if person_seen and vehicle_seen:
        severity = 2
        if re.search(r"\b(close|near)\b", text):
            severity = 3
        if re.search(r"\b(braking|swerving|swerve|stepping back|evasive)\b", text):
            severity = 4
        if re.search(r"\b(contact|collision|collided|struck)\b", text):
            severity = 5
    confidence = "medium" if persons > 0 and vehicles > 0 else "low"
    weak = confidence == "low"
    detection_text = _detection_text(candidate)
    rationale = (
        f'The description says "{candidate.description}". Detections: {detection_text}.'
    )
    if weak:
        rationale = "The evidence is weak. " + rationale
    rationale += " This score did not come from the model."
    return RankedHit(
        candidate=candidate,
        severity=severity,
        category=_category(text),
        rationale=rationale,
        people_visible=person_seen,
        faces_or_plates_possibly_visible=bool(re.search(r"\b(face|plate|licence|license)\b", text)),
        recommended_action=_action(severity),
        confidence=confidence,
        rule_based=True,
    )


def parse_rank_response(raw: str, segment_id: str) -> dict[str, Any]:
    """Extract and validate one ranking object."""
    data = extract_json(raw)
    if isinstance(data, list):
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValidationError("expected one object")
        data = data[0]
    if not isinstance(data, dict):
        raise ValidationError("expected an object")
    return validate_rank_object(data, segment_id)


def extract_json(raw: str) -> Any:
    """Parse a JSON object or array, including one wrapped in prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise ValidationError("invalid json") from exc
        raise ValidationError("invalid json")


def validate_rank_object(obj: dict[str, Any], segment_id: str) -> dict[str, Any]:
    """Return a normalised ranking object or raise ValidationError."""
    if obj.get("segment_id") != segment_id:
        raise ValidationError("segment_id mismatch")
    try:
        severity = int(obj["severity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("severity") from exc
    if severity not in range(1, 6):
        raise ValidationError("severity")
    category = str(obj.get("category") or "").lower()
    confidence = str(obj.get("confidence") or "").lower()
    if category not in CATEGORIES:
        raise ValidationError("category")
    if confidence not in CONFIDENCE:
        raise ValidationError("confidence")
    if not isinstance(obj.get("people_visible"), bool):
        raise ValidationError("people_visible")
    if not isinstance(obj.get("faces_or_plates_possibly_visible"), bool):
        raise ValidationError("faces_or_plates_possibly_visible")
    rationale = str(obj.get("rationale") or "").strip()
    action = str(obj.get("recommended_action") or "").strip()
    if not rationale or not action:
        raise ValidationError("text")
    return {
        "segment_id": segment_id,
        "severity": severity,
        "category": category,
        "rationale": rationale,
        "people_visible": obj["people_visible"],
        "faces_or_plates_possibly_visible": obj["faces_or_plates_possibly_visible"],
        "recommended_action": action,
        "confidence": confidence,
    }


def repair_prompt(candidate: Candidate, invalid: str) -> str:
    """Ask the model to return the same judgement as valid JSON."""
    return (
        "The previous reply was not valid JSON for this schema. "
        "Return one JSON object and nothing else. Keep segment_id exactly as given.\n"
        f"segment_id: {candidate.segment_id}\n"
        f"Invalid reply:\n{invalid[:2000]}"
    )


def _rank_one(candidate: Candidate, complete: Complete) -> RankedHit:
    prompt = _evidence_prompt(candidate)
    try:
        raw = complete(prompt)
    except LlmError:
        logger.info("ranking fallback segment=%s", candidate.segment_id)
        return rule_rank(candidate)
    try:
        parsed = parse_rank_response(raw, candidate.segment_id)
    except ValidationError:
        try:
            repaired = complete(repair_prompt(candidate, raw))
            parsed = parse_rank_response(repaired, candidate.segment_id)
        except (LlmError, ValidationError):
            logger.info("ranking repair failed segment=%s", candidate.segment_id)
            return rule_rank(candidate)
    return RankedHit(
        candidate=candidate,
        severity=int(parsed["severity"]),
        category=str(parsed["category"]),
        rationale=str(parsed["rationale"]),
        people_visible=bool(parsed["people_visible"]),
        faces_or_plates_possibly_visible=bool(parsed["faces_or_plates_possibly_visible"]),
        recommended_action=str(parsed["recommended_action"]),
        confidence=str(parsed["confidence"]),
        rule_based=False,
    )


def _model_complete(prompt: str) -> str:
    return llm.complete(SYSTEM, prompt, json_mode=True)


def _evidence_prompt(candidate: Candidate) -> str:
    detections = [item.to_dict() for item in candidate.detections]
    return (
        f"segment_id: {candidate.segment_id}\n"
        f"camera_id: {candidate.camera_id}\n"
        f"location: {candidate.location}\n"
        f"description: {candidate.description}\n"
        f"detections: {json.dumps(detections)}\n"
        "Return the JSON object for this segment only."
    )


def _count(candidate: Candidate, labels: set[str]) -> int:
    total = 0
    for detection in candidate.detections:
        if detection.label.lower() in labels:
            total += detection.count
    return total


def _mentions_person(text: str) -> bool:
    if re.search(r"\bno (person|people|pedestrian)\b", text):
        return False
    return bool(re.search(r"\b(person|pedestrian|people)\b", text))


def _detection_text(candidate: Candidate) -> str:
    if not candidate.detections:
        return "none"
    return ", ".join(f"{item.label} x{item.count}" for item in candidate.detections)


def _category(text: str) -> str:
    if any(word in text for word in ("forklift", "warehouse", "aisle")):
        return "warehouse"
    if any(word in text for word in ("street", "pedestrian")):
        return "street"
    if "highway" in text:
        return "highway"
    if "indoor" in text:
        return "indoor"
    return "other"


def _action(severity: int) -> str:
    actions = {
        1: "No further action. Keep the segment for the record.",
        2: "Monitor the area. The footage does not show a close approach.",
        3: "Review the close approach with the people on site.",
        4: "Review the evasive action and keep people clear of the vehicle path until it is checked.",
        5: "Secure the area and have a person review the footage before anyone else uses it.",
    }
    return actions.get(severity, actions[1])


def _sort_key(hit: RankedHit) -> tuple[int, int, float]:
    return (
        -hit.severity,
        -CONFIDENCE_RANK.get(hit.confidence, 0),
        -hit.candidate.search_score,
    )


def _fill_key(hit: RankedHit) -> tuple[int, float]:
    return (-hit.severity, -hit.candidate.search_score)
