from backend.models import Candidate, Clip, Detection, EvidenceHit, RankedHit
from backend.reports import (
    DISCLAIMER,
    finalize_german_text,
    incident_report,
    ticket_payload,
    translate_report,
)


def _hit(next_clip: bool = False) -> EvidenceHit:
    candidate = Candidate(
        segment_id="seg-wh-1",
        video_id="vid-wh-1",
        camera_id="wh-cam-1",
        location="Warehouse aisle 2",
        start=12,
        end=20,
        description="A person is close to a moving forklift that is braking in the aisle.",
        detections=[Detection("person", 1), Detection("forklift", 1)],
        search_score=0.91,
        clip_url="api/fixture/sample.mp4",
    )
    ranked = RankedHit(
        candidate=candidate,
        severity=4,
        category="warehouse",
        rationale="The description says the forklift is braking.",
        people_visible=True,
        faces_or_plates_possibly_visible=False,
        recommended_action="Review the evasive action.",
        confidence="medium",
        rule_based=True,
    )
    clips = {
        "previous": Clip("prev", 4, 12, "api/fixture/sample.mp4").to_dict(),
        "event": Clip("seg-wh-1", 12, 20, "api/fixture/sample.mp4").to_dict(),
        "next": Clip("next", 20, 28, "api/fixture/sample.mp4").to_dict() if next_clip else None,
    }
    return EvidenceHit(
        ranked=ranked,
        clips=clips,
        missing_neighbours=[] if next_clip else ["next"],
        privacy_flags=["people visible"],
    )


def test_english_report_cites_segment_and_time_range():
    report = incident_report(_hit())
    assert "seg-wh-1" in report.text
    assert "12" in report.text
    assert "20" in report.text
    assert report.summary.count(".") >= 2


def test_german_formatting_keeps_ids_and_one_disclaimer():
    text = finalize_german_text(
        "Segment seg-wh-1 bleibt 4.\n\n" + DISCLAIMER + "\nextra\n" + DISCLAIMER,
        "seg-wh-1",
    )
    assert text.count("seg-wh-1") == 1
    assert " 4." in text or text.startswith("Segment seg-wh-1 bleibt 4.")
    assert text.count(DISCLAIMER) == 1


def test_german_template_includes_disclaimer_and_segment_id():
    hit = _hit()
    report = translate_report(incident_report(hit), hit=hit)
    assert report.segment_id == "seg-wh-1"
    assert "seg-wh-1" in report.text
    assert report.text.count(DISCLAIMER) == 1
    assert "Sie" in report.text
    assert "12" in report.text and "20" in report.text


def test_ticket_has_null_assignee_and_does_not_post_without_a_webhook():
    hit = _hit(next_clip=False)
    called = {"n": 0}

    def poster(_url: str, _payload: dict) -> None:
        called["n"] += 1

    ticket = ticket_payload(hit, incident_report(hit), poster=poster)
    assert called["n"] == 0
    assert ticket["assignee"] is None
    assert ticket["posted"] is False
    assert ticket["clip_urls"]["event"] == "api/fixture/sample.mp4"
    assert ticket["clip_urls"]["next"] is None
