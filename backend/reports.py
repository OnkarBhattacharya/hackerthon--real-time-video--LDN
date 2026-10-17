"""English incident reports, German translation, and ticket payloads."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from backend.config import load_settings
from backend.evidence import GDPR_NOTE, EvidenceBundle
from backend.llm import LlmClient, LlmError

log = logging.getLogger(__name__)

GERMAN_DISCLAIMER = "Zur manuellen Prüfung; keine Rechtsberatung."

SECTION_ORDER = (
    "Summary",
    "What the footage shows",
    "Severity and why",
    "Location and camera",
    "Time window",
    "Privacy review items",
    "Recommended actions",
)

GERMAN_HEADINGS = {
    "Summary": "Zusammenfassung",
    "What the footage shows": "Was das Videomaterial zeigt",
    "Severity and why": "Schweregrad und Begründung",
    "Location and camera": "Standort und Kamera",
    "Time window": "Zeitfenster",
    "Privacy review items": "Datenschutz-Prüfpunkte",
    "Recommended actions": "Empfohlene Maßnahmen",
}

_TRANSLATE_SYSTEM = """You translate incident-report sections into formal German (de-DE).
Rules:
- Address the reader with formal Sie. Do not use du.
- Use metric units. Do not invent a unit conversion when the source has no unit.
- Use 24-hour time. Do not invent a clock time from a clip offset in seconds.
- If a calendar date is present, write it as DD.MM.YYYY. Do not invent a date.
- Copy every placeholder of the form [[K0]] exactly, brackets and digits included.
- Do not add people, vehicles, contact, causes, or places that are not in the source.
- Return one JSON object: {"sections": {"<same key>": "<translated value>", ...}}.
- Keep the input section keys unchanged. Translate only the values.
"""

_WEAK = re.compile(r"\b(weak|insufficient|unclear)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


class ReportError(RuntimeError):
    """Report building or translation failed."""


class TicketWebhookError(RuntimeError):
    """Ticket webhook POST failed."""


@dataclass
class IncidentReport:
    """One language version of an incident report."""

    language: str
    segment_id: str
    camera_id: str
    location: str
    start: float
    end: float
    severity: int
    summary: str
    sections: dict[str, str]
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "segment_id": self.segment_id,
            "camera_id": self.camera_id,
            "location": self.location,
            "start": self.start,
            "end": self.end,
            "severity": self.severity,
            "summary": self.summary,
            "sections": dict(self.sections),
            "text": self.text,
        }


def incident_report(hit: EvidenceBundle) -> IncidentReport:
    """Build an English report that cites only the evidence on the hit."""
    candidate = hit.hit.candidate
    sections = {
        "Summary": _summary(hit),
        "What the footage shows": _footage(hit),
        "Severity and why": _severity_why(hit),
        "Location and camera": (
            f"Location: {candidate.location}. Camera: {candidate.camera_id}."
        ),
        "Time window": _time_window(candidate.segment_id, candidate.start, candidate.end),
        "Privacy review items": _privacy(hit),
        "Recommended actions": _action(hit),
    }
    text = render_sections(sections)
    log.info(
        "report.english camera=%s severity=%s",
        candidate.camera_id,
        hit.hit.severity,
    )
    return IncidentReport(
        language="en",
        segment_id=candidate.segment_id,
        camera_id=candidate.camera_id,
        location=candidate.location,
        start=candidate.start,
        end=candidate.end,
        severity=hit.hit.severity,
        summary=sections["Summary"],
        sections=sections,
        text=text,
    )


def translate_report(
    report: IncidentReport,
    target: str = "de-DE",
    *,
    llm: LlmClient | None = None,
) -> IncidentReport:
    """Translate a report into formal German. Segment ids and numbers stay unchanged.

    Raises ReportError when the model is unavailable or the translation is incomplete.
    """
    if target not in {"de", "de-DE"}:
        raise ReportError(f"Unsupported translation target: {target}")
    client = llm if llm is not None else LlmClient()
    last_error: ReportError | None = None
    for attempt in (1, 2):
        try:
            translated = _translate_once(report, client)
            log.info("report.german camera=%s target=%s", report.camera_id, target)
            return translated
        except ReportError as exc:
            last_error = exc
            log.warning("report.german attempt=%s failed=%s", attempt, type(exc).__name__)
    assert last_error is not None
    raise last_error


def ticket_payload(
    hit: EvidenceBundle,
    report: IncidentReport,
    *,
    webhook_url: str | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Build the ticket JSON. POST it only when a webhook URL is configured.

    An empty or missing URL returns the payload and does not open a connection.
    A webhook failure raises TicketWebhookError.
    """
    summary = report.summary if report.language == "en" else incident_report(hit).summary
    payload: dict[str, Any] = {
        "title": f"Severity {hit.hit.severity} review on {hit.hit.candidate.camera_id}",
        "severity": hit.hit.severity,
        "camera_id": hit.hit.candidate.camera_id,
        "location": hit.hit.candidate.location,
        "time_window": {
            "start": hit.hit.candidate.start,
            "end": hit.hit.candidate.end,
            "segment_id": hit.hit.candidate.segment_id,
        },
        "summary": summary,
        "clip_urls": _clip_urls(hit),
        "privacy_flags": list(hit.privacy_flags),
        "assignee": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    url = webhook_url
    if url is None:
        url = load_settings().ticket_webhook_url
    url = (url or "").strip()
    if not url:
        log.info("ticket.webhook skipped")
        return payload
    timeout = timeout_sec if timeout_sec is not None else load_settings().request_timeout_sec
    _post_webhook(payload, url, timeout)
    return payload


def render_sections(sections: dict[str, str]) -> str:
    """Render titled sections in insertion order."""
    return "\n\n".join(f"{title}\n{body.strip()}" for title, body in sections.items())


def format_offset(value: float) -> str:
    """Render a clip offset in seconds without trailing zeros."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def shield_literals(
    text: str,
    literals: list[str],
    *,
    index: int = 0,
) -> tuple[str, dict[str, str], int]:
    """Replace exact literals with [[K0]] tokens so a translator cannot reformat them."""
    ordered = sorted({lit for lit in literals if lit}, key=len, reverse=True)
    if not ordered:
        return text, {}, index
    mapping: dict[str, str] = {}
    pattern = re.compile("|".join(re.escape(lit) for lit in ordered))
    counter = index

    def repl(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"[[K{counter}]]"
        mapping[token] = match.group(0)
        counter += 1
        return token

    return pattern.sub(repl, text), mapping, counter


def restore_literals(text: str, mapping: dict[str, str]) -> str:
    """Put original segment ids and numbers back in place of shield tokens."""
    restored = text
    for token, literal in mapping.items():
        restored = restored.replace(token, literal)
    return restored


def preserve_literals(text: str, literals: list[str]) -> str:
    """Shield and restore literals. The text is unchanged when no translator runs."""
    shielded, mapping, _index = shield_literals(text, literals)
    return restore_literals(shielded, mapping)


def append_disclaimer(text: str) -> str:
    """Append the German disclaimer so it appears exactly once."""
    body = text.replace(GERMAN_DISCLAIMER, "").strip()
    return f"{body}\n\n{GERMAN_DISCLAIMER}"


def _summary(hit: EvidenceBundle) -> str:
    candidate = hit.hit.candidate
    first = (
        f"Camera {candidate.camera_id} at {candidate.location} is severity "
        f"{hit.hit.severity} ({hit.hit.category}) for segment {candidate.segment_id} "
        f"from {format_offset(candidate.start)} s to {format_offset(candidate.end)} s."
    )
    rationale = " ".join((hit.hit.rationale or "").split())
    if not rationale:
        second = "No rationale was provided with this hit."
    else:
        second = _as_one_sentence(rationale)
        if _WEAK.search(rationale) and not _WEAK.search(second):
            second = _as_one_sentence(f"The evidence is weak. {rationale}")
    return f"{first} {second}"


def _footage(hit: EvidenceBundle) -> str:
    candidate = hit.hit.candidate
    description = " ".join((candidate.description or "").split())
    if not description:
        description = "No description was provided."
    if candidate.detections:
        listed = ", ".join(
            f"{item.class_name} ({item.count})" for item in candidate.detections
        )
    else:
        listed = "none recorded"
    return (
        f"Description: {description} "
        f"Detections: {listed}. "
        f"{_time_window(candidate.segment_id, candidate.start, candidate.end)}"
    )


def _severity_why(hit: EvidenceBundle) -> str:
    rationale = " ".join((hit.hit.rationale or "").split()) or "No rationale was provided."
    return (
        f"Severity {hit.hit.severity} ({hit.hit.category}), "
        f"confidence {hit.hit.confidence}. {rationale}"
    )


def _privacy(hit: EvidenceBundle) -> str:
    flags = list(hit.privacy_flags)
    if GDPR_NOTE not in flags:
        flags.append(GDPR_NOTE)
    return "\n".join(f"- {flag}" for flag in flags)


def _action(hit: EvidenceBundle) -> str:
    action = " ".join((hit.hit.recommended_action or "").split())
    return action or "No recommended action was provided."


def _time_window(segment_id: str, start: float, end: float) -> str:
    return (
        f"Segment {segment_id} from {format_offset(start)} s to {format_offset(end)} s."
    )


def _as_one_sentence(text: str) -> str:
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()]
    clauses = [part.rstrip(".!?").strip() for part in parts]
    clauses = [clause for clause in clauses if clause]
    if not clauses:
        return "No rationale was provided."
    return "; ".join(clauses) + "."


def _clip_urls(hit: EvidenceBundle) -> dict[str, str | None]:
    urls: dict[str, str | None] = {}
    for role in ("previous", "event", "next"):
        slot = hit.triptych.get(role)
        if slot is None or not slot.present or not slot.clip_url:
            urls[role] = None
        else:
            urls[role] = slot.clip_url
    return urls


def _literals_for(report: IncidentReport) -> list[str]:
    blob = "\n".join(report.sections.values())
    literals = [
        report.segment_id,
        report.camera_id,
        report.location,
        format_offset(report.start),
        format_offset(report.end),
        str(report.severity),
        *_NUMBER.findall(blob),
    ]
    return [item for item in literals if item]


def _translate_once(report: IncidentReport, client: LlmClient) -> IncidentReport:
    literals = _literals_for(report)
    shielded: dict[str, str] = {}
    mapping: dict[str, str] = {}
    index = 0
    for key in SECTION_ORDER:
        hidden, part_map, index = shield_literals(report.sections[key], literals, index=index)
        shielded[key] = hidden
        mapping.update(part_map)
    try:
        raw = client.chat_json(
            system=_TRANSLATE_SYSTEM,
            user=json.dumps({"sections": shielded}, ensure_ascii=False),
            max_tokens=1800,
            temperature=0.0,
        )
    except LlmError as exc:
        raise ReportError(f"German translation failed: {exc}") from exc
    sections = _german_sections(report, raw, mapping)
    display = {GERMAN_HEADINGS[key]: sections[key] for key in SECTION_ORDER}
    text = append_disclaimer(render_sections(display))
    if report.segment_id not in text:
        raise ReportError("German report is missing the segment id")
    if format_offset(report.start) not in text or format_offset(report.end) not in text:
        raise ReportError("German report changed the time range")
    if text.count(GERMAN_DISCLAIMER) != 1:
        raise ReportError("German disclaimer was not applied once")
    return IncidentReport(
        language="de-DE",
        segment_id=report.segment_id,
        camera_id=report.camera_id,
        location=report.location,
        start=report.start,
        end=report.end,
        severity=report.severity,
        summary=sections["Summary"],
        sections=sections,
        text=text,
    )


def _german_sections(
    report: IncidentReport,
    raw: dict[str, Any],
    mapping: dict[str, str],
) -> dict[str, str]:
    payload = raw.get("sections") if isinstance(raw, dict) else None
    if not isinstance(payload, dict):
        raise ReportError("German translation did not return sections")
    restored: dict[str, str] = {}
    for key in SECTION_ORDER:
        value = _section_value(payload, key)
        if value is None:
            raise ReportError(f"German translation missed section: {key}")
        restored[key] = restore_literals(value, mapping).strip()
        if not restored[key]:
            raise ReportError(f"German translation missed section: {key}")
    prose = (
        "Summary",
        "What the footage shows",
        "Severity and why",
        "Privacy review items",
        "Recommended actions",
    )
    for key in prose:
        if "[[K" in restored[key]:
            raise ReportError("German translation dropped a protected token")
    unchanged = [
        key for key in prose if restored[key] == report.sections[key].strip()
    ]
    if len(unchanged) >= 3:
        raise ReportError("German translation left the report in English")
    restored["Time window"] = (
        f"Segment {report.segment_id} von {format_offset(report.start)} s "
        f"bis {format_offset(report.end)} s."
    )
    restored["Location and camera"] = (
        f"Standort: {report.location}. Kamera: {report.camera_id}."
    )
    if str(report.severity) not in restored["Severity and why"]:
        restored["Severity and why"] = (
            f"Schweregrad {report.severity}. {restored['Severity and why']}"
        )
    return restored


def _section_value(payload: dict[str, Any], key: str) -> str | None:
    if isinstance(payload.get(key), str):
        return str(payload[key])
    for raw_key, value in payload.items():
        if str(raw_key).strip().lower() == key.lower() and isinstance(value, str):
            return value
    return None


def _post_webhook(payload: dict[str, Any], url: str, timeout_sec: float) -> None:
    """POST the ticket. Logs status only; never logs the URL or payload."""
    log.info("ticket.webhook posting timeout_sec=%s", timeout_sec)
    try:
        response = requests.post(url, json=payload, timeout=timeout_sec)
    except requests.RequestException as exc:
        log.warning("ticket.webhook failed error=%s", type(exc).__name__)
        raise TicketWebhookError(
            f"Ticket webhook request failed ({type(exc).__name__})"
        ) from exc
    log.info("ticket.webhook status=%s", response.status_code)
    if response.status_code < 200 or response.status_code >= 300:
        raise TicketWebhookError(f"Ticket webhook returned HTTP {response.status_code}")
