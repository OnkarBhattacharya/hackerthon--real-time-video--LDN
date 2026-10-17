import json

from backend.models import Candidate, Detection, RankedHit
from backend.ranking import parse_rank_response, rank_candidates, rule_rank, select_hits


def _candidate(description: str, detections: list[Detection] | None = None, score: float = 0.5) -> Candidate:
    return Candidate(
        segment_id="seg-1",
        video_id="vid",
        camera_id="cam",
        location="Warehouse aisle",
        start=1,
        end=2,
        description=description,
        detections=detections or [],
        search_score=score,
        clip_url="",
    )


def _ranked(segment_id: str, severity: int, confidence: str, score: float) -> RankedHit:
    candidate = _candidate("person near a vehicle", score=score)
    candidate.segment_id = segment_id
    return RankedHit(
        candidate=candidate,
        severity=severity,
        category="other",
        rationale="cited",
        people_visible=True,
        faces_or_plates_possibly_visible=False,
        recommended_action="Review it.",
        confidence=confidence,
        rule_based=True,
    )


def _valid(segment_id: str = "seg-1", severity: int = 4) -> str:
    return json.dumps(
        {
            "segment_id": segment_id,
            "severity": severity,
            "category": "warehouse",
            "rationale": "The description shows a close approach.",
            "people_visible": True,
            "faces_or_plates_possibly_visible": False,
            "recommended_action": "Review the approach.",
            "confidence": "high",
        }
    )


def test_valid_json_is_accepted():
    calls = {"n": 0}

    def complete(prompt: str) -> str:
        calls["n"] += 1
        return _valid()

    ranked = rank_candidates([_candidate("person close to a forklift")], complete=complete)
    assert calls["n"] == 1
    assert ranked[0].rule_based is False
    assert ranked[0].severity == 4


def test_malformed_json_is_repaired_once():
    calls = {"n": 0}

    def complete(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json"
        return _valid()

    ranked = rank_candidates([_candidate("person close to a forklift")], complete=complete)
    assert calls["n"] == 2
    assert ranked[0].severity == 4
    assert ranked[0].rule_based is False


def test_still_invalid_json_uses_rules():
    ranked = rank_candidates([_candidate("person close to a forklift")], complete=lambda _prompt: "still bad")
    assert ranked[0].rule_based is True


def test_sort_order_is_severity_then_confidence_then_score():
    ordered = select_hits(
        [
            _ranked("a", 3, "high", 0.9),
            _ranked("b", 4, "low", 0.1),
            _ranked("c", 4, "high", 0.2),
            _ranked("d", 4, "high", 0.8),
        ]
    )
    assert [hit.candidate.segment_id for hit in ordered] == ["d", "c", "b", "a"]


def test_keeps_six_when_few_are_severe():
    hits = [
        _ranked("s5", 5, "low", 0.1),
        _ranked("s4", 4, "low", 0.1),
        _ranked("s3", 3, "low", 0.1),
        _ranked("s2a", 2, "low", 0.9),
        _ranked("s2b", 2, "low", 0.4),
        _ranked("s1a", 1, "low", 0.8),
        _ranked("s1b", 1, "low", 0.2),
    ]
    selected = select_hits(hits)
    assert len(selected) == 6
    assert {hit.candidate.segment_id for hit in selected} >= {"s5", "s4", "s3", "s2a"}


def test_rule_score_rises_for_a_close_approach_and_stays_low_without_a_person():
    close = rule_rank(
        _candidate(
            "A person is close to a moving forklift.",
            [Detection("person", 1), Detection("forklift", 1)],
        )
    )
    empty = rule_rank(_candidate("An empty road with traffic lights only."))
    braking = rule_rank(
        _candidate(
            "A vehicle is braking while a person stands in the lane.",
            [Detection("person", 1), Detection("vehicle", 1)],
        )
    )
    assert close.severity >= 3
    assert close.severity > empty.severity
    assert empty.severity == 1
    assert braking.severity >= 4


def test_parse_rank_response_rejects_the_wrong_segment():
    raw = _valid("other-id")
    try:
        parse_rank_response(raw, "seg-1")
    except Exception as exc:
        assert exc.__class__.__name__ == "ValidationError"
    else:
        raise AssertionError("expected a validation error")
