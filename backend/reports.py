"""English reports, German translation, and ticket payloads."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.config import settings
from backend.llm import LlmError, llm
from backend.models import EvidenceHit

logger = logging.getLogger(__name__)

DISCLAIMER = "Zur manuellen Prüfung; keine Rechtsberatung."

Poster = Callable[[str, dict[str, Any]], None]


class ReportFormatError(Exception):
    """A generated report dropped a required identifier."""


class TicketWebhookError(Exception):
    """The webhook call failed. The message does not include the URL."""


class IncidentReport:
    """One rendered report."""

    def __init__(self, lang: str, segment_id: str, text: str, summary: str, source: str) -> None:
        self.lang = lang
        self.segment_id = segment_id
        self.text = text
        self.summary = summary
        self.source = source

    def to_dict(self) -> dict[str, str]:
        """Return the API body."""
        return {
            "lang": self.lang,
            "segment_id": self.segment_id,
            "text": self.text,
            "summary": self.summary,
            "source": self.source,
        }


def incident_report(hit: EvidenceHit) -> IncidentReport:
    """English report that cites only the evidence on the hit."""
    candidate = hit.ranked.candidate
    start = format_offset(candidate.start)
    end = format_offset(candidate.end)
    weak = "evidence is weak" in hit.ranked.rationale.lower()
    second = f"The ranked severity is {hit.ranked.severity}."
    if weak:
        second = f"The evidence is weak and the ranked severity is {hit.ranked.severity}."
    summary = (
        f"Segment {candidate.segment_id} on camera {candidate.camera_id} "
        f"covers {start} to {end} seconds. {second}"
    )
    detections = _detection_line(hit)
    sections = [
        ("Summary", summary),
        (
            "What the footage shows",
            f"{candidate.description}\nDetections: {detections}.\nTime range: {start} to {end} seconds.",
        ),
        (
            "Severity and why",
            f"Severity {hit.ranked.severity}. {hit.ranked.rationale}",
        ),
        ("Location and camera", f"{candidate.location}. Camera {candidate.camera_id}."),
        (
            "Time window",
            f"Segment {candidate.segment_id}, {start}–{end} seconds.",
        ),
        (
            "Privacy review items",
            _privacy_block(hit.privacy_flags, hit.privacy_note),
        ),
        ("Recommended actions", hit.ranked.recommended_action),
    ]
    text = "\n\n".join(f"{title}\n{body}" for title, body in sections)
    return IncidentReport("en", candidate.segment_id, text, summary, "template")


def translate_report(
    report: IncidentReport,
    target: str = "de-DE",
    hit: EvidenceHit | None = None,
) -> IncidentReport:
    """German report. Segment ids and numbers stay unchanged."""
    if target != "de-DE":
        raise ReportFormatError(target)
    if hit is None:
        raise ReportFormatError("hit")
    segment_id = hit.ranked.candidate.segment_id
    if llm.configured():
        try:
            raw = llm.complete(_GERMAN_SYSTEM, _german_user(report, hit), json_mode=False)
            text = finalize_german_text(raw, segment_id)
            return IncidentReport("de", segment_id, text, report.summary, "model")
        except (LlmError, ReportFormatError):
            logger.info("german report fallback segment=%s", segment_id)
    text = finalize_german_text(german_template(hit), segment_id)
    return IncidentReport("de", segment_id, text, report.summary, "template")


def ticket_payload(
    hit: EvidenceHit,
    report: IncidentReport,
    poster: Poster | None = None,
) -> dict[str, Any]:
    """JSON ticket. POST only when TICKET_WEBHOOK_URL is set."""
    candidate = hit.ranked.candidate
    payload: dict[str, Any] = {
        "title": f"Severity {hit.ranked.severity} on {candidate.camera_id}",
        "severity": hit.ranked.severity,
        "camera_id": candidate.camera_id,
        "location": candidate.location,
        "time_window": {
            "start": candidate.start,
            "end": candidate.end,
            "segment_id": candidate.segment_id,
        },
        "summary": report.summary,
        "clip_urls": {
            "previous": _clip_url(hit, "previous"),
            "event": _clip_url(hit, "event"),
            "next": _clip_url(hit, "next"),
        },
        "privacy_flags": list(hit.privacy_flags),
        "assignee": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    url = settings.ticket_webhook_url()
    if not url:
        payload["posted"] = False
        return payload
    sender = poster or post_ticket
    sender(url, payload)
    payload["posted"] = True
    return payload


def post_ticket(url: str, payload: dict[str, Any]) -> None:
    """POST a ticket. Failures raise TicketWebhookError and omit the URL."""
    started = time.perf_counter()
    try:
        response = httpx.post(url, json=payload, timeout=15)
    except httpx.HTTPError as exc:
        logger.info("ticket webhook status=error elapsed=%.2f", time.perf_counter() - started)
        raise TicketWebhookError("The ticket webhook could not be reached.") from exc
    logger.info(
        "ticket webhook status=%s elapsed=%.2f",
        response.status_code,
        time.perf_counter() - started,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise TicketWebhookError("The ticket webhook rejected the payload.")


def finalize_german_text(text: str, segment_id: str) -> str:
    """Keep the segment id intact and include the disclaimer once."""
    if segment_id not in text:
        raise ReportFormatError(segment_id)
    body = text.replace(DISCLAIMER, "").rstrip()
    return f"{body}\n\n{DISCLAIMER}\n"


def german_template(hit: EvidenceHit) -> str:
    """Formal German report built only from the hit fields."""
    candidate = hit.ranked.candidate
    start = format_offset(candidate.start)
    end = format_offset(candidate.end)
    severity = hit.ranked.severity
    flags = ", ".join(_flag_de(flag) for flag in hit.privacy_flags) or "keine zusätzlichen Hinweise"
    return (
        "Zusammenfassung\n"
        f"Segment {candidate.segment_id} an der Kamera {candidate.camera_id} "
        f"umfasst {start} bis {end} Sekunden. "
        f"Der Schweregrad ist {severity}. "
        "Diese Fassung ist eine Vorlagenübersetzung, weil das Modell nicht verfügbar ist.\n\n"
        "Was die Aufnahmen zeigen\n"
        f"{candidate.description}\n"
        f"Erkennungen: {_detection_line(hit)}.\n"
        f"Zeitraum: {start} bis {end} Sekunden.\n\n"
        "Schweregrad und Begründung\n"
        f"Schweregrad {severity}. {hit.ranked.rationale}\n\n"
        "Ort und Kamera\n"
        f"{candidate.location}. Kamera {candidate.camera_id}.\n\n"
        "Zeitfenster\n"
        f"Segment {candidate.segment_id}, {start}–{end} Sekunden.\n\n"
        "Datenschutzprüfung\n"
        f"{flags}.\n"
        "Bitte prüfen Sie die Aufnahme vor dem Teilen. Mögliche personenbezogene Daten (DSGVO).\n\n"
        "Empfohlene Maßnahmen\n"
        f"{_action_de(severity)}"
    )


def format_offset(value: float) -> str:
    """Format a segment offset without changing an integer value."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def _detection_line(hit: EvidenceHit) -> str:
    items = hit.ranked.candidate.detections
    if not items:
        return "none"
    return ", ".join(f"{item.label} x{item.count}" for item in items)


def _privacy_block(flags: list[str], note: str) -> str:
    lines = "\n".join(f"- {flag}" for flag in flags) or "- none recorded"
    return f"{lines}\n{note}"


def _clip_url(hit: EvidenceHit, role: str) -> str | None:
    clip = hit.clips.get(role)
    if not isinstance(clip, dict):
        return None
    url = clip.get("clip_url")
    return str(url) if url else None


def _flag_de(flag: str) -> str:
    if flag == "people visible":
        return "Personen sichtbar"
    if flag == "faces or licence plates possibly visible":
        return "Gesichter oder Kennzeichen möglicherweise sichtbar"
    return flag


def _action_de(severity: int) -> str:
    actions = {
        1: "Keine weitere Maßnahme. Bewahren Sie das Segment nur zur Dokumentation auf.",
        2: "Beobachten Sie die Stelle. Die Aufnahme zeigt keine enge Annäherung.",
        3: "Prüfen Sie die Annäherung mit den Personen vor Ort.",
        4: "Prüfen Sie das Ausweichverhalten und halten Sie Personen vom Fahrweg fern, bis die Stelle geprüft ist.",
        5: "Sichern Sie die Stelle und lassen Sie die Aufnahme durch eine Person prüfen.",
    }
    return actions.get(severity, actions[1])


_GERMAN_SYSTEM = (
    "Übersetzen Sie den Bericht ins Deutsche. Verwenden Sie die formelle Anrede Sie. "
    "Verwenden Sie metrische Einheiten, 24-Stunden-Zeit und das Datum DD.MM.YYYY, "
    "wenn ein Kalenderdatum vorhanden ist. Erfinden Sie kein Datum. "
    "Segment-IDs, Kamera-IDs und alle Zahlen bleiben unverändert. "
    f"Schließen Sie mit der Zeile: {DISCLAIMER}"
)


def _german_user(report: IncidentReport, hit: EvidenceHit) -> str:
    candidate = hit.ranked.candidate
    return (
        f"segment_id: {candidate.segment_id}\n"
        f"camera_id: {candidate.camera_id}\n"
        f"severity: {hit.ranked.severity}\n"
        f"start: {format_offset(candidate.start)}\n"
        f"end: {format_offset(candidate.end)}\n\n"
        f"{report.text}"
    )
