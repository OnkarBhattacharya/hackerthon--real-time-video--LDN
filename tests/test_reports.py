"""Pure-logic tests for incident reports and ticket payloads."""

from __future__ import annotations

import json
import re

import pytest
import requests

from backend.evidence import GDPR_NOTE, EvidenceBundle, TriptychSlot
from backend.models import Candidate, Detection, RankedHit
from backend.reports import (
    GERMAN_DISCLAIMER,
    IncidentReport,
    ReportError,
    TicketWebhookError,
    append_disclaimer,
    incident_report,
    preserve_literals,
    restore_literals,
    shield_literals,
    ticket_payload,
    translate_report,
)

_TICKET_KEYS = {
    "title",
    "severity",
    "camera_id",
    "location",
    "time_window",
    "summary",
    "clip_urls",
    "privacy_flags",
    "assignee",
    "created_at",
}


def _bundle(
    *,
    description: str = "A person stands near a forklift.",
    detections: list[Detection] | None = None,
    previous: bool = False,
    rationale: str = "The person is close to the forklift. Evidence is weak on contact.",
) -> EvidenceBundle:
    if detections is None:
        detections = [
            Detection(class_name="person", count=1),
            Detection(class_name="forklift", count=1),
        ]
    candidate = Candidate(
        segment_id="warehouse/seg-42",
        video_id="vid-1",
        camera_id="sdg_warehouse_cam-2",
        location="San Diego warehouse",
        start=12.5,
        end=18.5,
        description=description,
        detections=detections,
        search_score=0.42,
        clip_url="https://example.test/self?token=secret",
        segment_number=3,
    )
    ranked = RankedHit(
        candidate=candidate,
        severity=4,
        category="warehouse",
        rationale=rationale,
        people_visible=True,
        faces_or_plates_possibly_visible=False,
        recommended_action="Review the clip before sharing.",
        confidence="low",
        ranking_source="llm",
    )
    return EvidenceBundle(
        hit=ranked,
        triptych={
            "previous": TriptychSlot(role="previous", present=previous, clip_url=None),
            "event": TriptychSlot(
                role="event",
                present=True,
                clip_url="https://example.test/event",
                start=12.5,
                end=18.5,
            ),
            "next": TriptychSlot(
                role="next",
                present=True,
                clip_url="https://example.test/next",
            ),
        },
        neighbour_flags=[] if previous else ["missing_previous"],
        privacy_flags=["people visible", GDPR_NOTE],
    )


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part]


def test_english_report_cites_segment_and_timestamps() -> None:
    report = incident_report(_bundle())
    assert report.language == "en"
    assert report.segment_id in report.text
    assert "12.5" in report.text
    assert "18.5" in report.text
    assert "sdg_warehouse_cam-2" in report.text
    assert "San Diego warehouse" in report.text
    assert "Severity 4" in report.sections["Severity and why"]
    assert GDPR_NOTE in report.sections["Privacy review items"]
    assert report.sections["Recommended actions"] == "Review the clip before sharing."
    assert _sentences(report.summary) == _sentences(report.sections["Summary"])
    assert len(_sentences(report.summary)) == 2
    assert "weak" in report.summary.lower()
    assert "token=" not in report.text
    assert "https://" not in report.text


def test_english_report_does_not_invent_objects() -> None:
    report = incident_report(
        _bundle(
            description="Empty corridor.",
            detections=[],
            rationale="No close approach is visible.",
        )
    )
    lowered = report.text.lower()
    assert "forklift" not in lowered
    assert "pedestrian" not in lowered
    assert "collision" not in lowered
    assert "none recorded" in lowered


def test_preserve_literals_keeps_segment_id_and_integers() -> None:
    segment_id = "warehouse/seg-42"
    text = f"Segment {segment_id} severity 4 from 12.5 s to 18.5 s."
    literals = [segment_id, "4", "12.5", "18.5"]
    assert preserve_literals(text, literals) == text
    shielded, mapping, _index = shield_literals(text, literals)
    assert segment_id not in shielded
    assert "12.5" not in shielded
    german = (
        shielded.replace("Segment", "Abschnitt")
        .replace("severity", "Schweregrad")
        .replace(" from ", " von ")
        .replace(" to ", " bis ")
    )
    restored = restore_literals(german, mapping)
    assert segment_id in restored
    assert re.search(r"\b4\b", restored)
    assert "12.5" in restored
    assert "18.5" in restored


def test_disclaimer_is_present_exactly_once() -> None:
    doubled = f"Hinweis.\n{GERMAN_DISCLAIMER}\n\n{GERMAN_DISCLAIMER}"
    text = append_disclaimer(doubled)
    assert text.count(GERMAN_DISCLAIMER) == 1
    assert text.endswith(GERMAN_DISCLAIMER)


def test_translate_report_restores_ids_and_adds_disclaimer() -> None:
    class FakeLlm:
        def __init__(self) -> None:
            self.system = ""

        def chat_json(
            self,
            *,
            system: str,
            user: str,
            max_tokens: int,
            temperature: float,
        ) -> dict:
            self.system = system
            assert max_tokens > 0
            assert temperature == 0.0
            payload = json.loads(user)
            return {
                "sections": {
                    key: f"Übersetzung {value}" for key, value in payload["sections"].items()
                }
            }

    fake = FakeLlm()
    report = translate_report(incident_report(_bundle()), llm=fake)  # type: ignore[arg-type]
    assert isinstance(report, IncidentReport)
    assert report.language == "de-DE"
    assert "warehouse/seg-42" in report.text
    assert "12.5" in report.text
    assert "18.5" in report.text
    assert report.text.count(GERMAN_DISCLAIMER) == 1
    assert "Zusammenfassung" in report.text
    assert "Sie" in fake.system
    assert "DD.MM.YYYY" in fake.system
    assert "[[K" not in report.text


def test_ticket_payload_null_assignee_and_missing_neighbour(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("webhook should not be called")

    monkeypatch.setattr("backend.reports.requests.post", fail_post)
    bundle = _bundle(previous=False)
    report = incident_report(bundle)
    payload = ticket_payload(bundle, report, webhook_url="")
    assert set(payload) == _TICKET_KEYS
    assert payload["assignee"] is None
    assert payload["severity"] == 4
    assert payload["camera_id"] == "sdg_warehouse_cam-2"
    assert payload["location"] == "San Diego warehouse"
    assert payload["summary"] == report.summary
    assert payload["privacy_flags"] == ["people visible", GDPR_NOTE]
    assert payload["clip_urls"] == {
        "previous": None,
        "event": "https://example.test/event",
        "next": "https://example.test/next",
    }
    assert payload["time_window"] == {
        "start": 12.5,
        "end": 18.5,
        "segment_id": "warehouse/seg-42",
    }
    parsed = payload["created_at"]
    assert isinstance(parsed, str) and "T" in parsed


def test_webhook_failure_raises_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_url = "https://hooks.example.test/secret-path"

    def fake_post(url: str, json: dict | None = None, timeout: float | None = None) -> object:
        assert url == secret_url
        assert timeout == 5
        assert json is not None

        class Response:
            status_code = 503

        return Response()

    monkeypatch.setattr("backend.reports.requests.post", fake_post)
    bundle = _bundle()
    with pytest.raises(TicketWebhookError) as exc:
        ticket_payload(
            bundle,
            incident_report(bundle),
            webhook_url=secret_url,
            timeout_sec=5,
        )
    assert "secret-path" not in str(exc.value)
    assert "503" in str(exc.value)


def test_webhook_connection_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*_args: object, **_kwargs: object) -> None:
        raise requests.ConnectionError("down")

    monkeypatch.setattr("backend.reports.requests.post", fake_post)
    bundle = _bundle()
    with pytest.raises(TicketWebhookError) as exc:
        ticket_payload(
            bundle,
            incident_report(bundle),
            webhook_url="https://hooks.example.test/hook",
            timeout_sec=3,
        )
    assert "hooks.example.test" not in str(exc.value)


def test_translate_rejects_unsupported_language() -> None:
    with pytest.raises(ReportError):
        translate_report(incident_report(_bundle()), target="fr-FR", llm=object())  # type: ignore[arg-type]
