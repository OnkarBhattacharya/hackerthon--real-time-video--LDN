# Stage 4 — Reports, German version, ticket

**Time box:** 30 minutes
**Depends on:** Stage 3 evidence hits
**Blocks:** the report and ticket API endpoints

Shared rules: [constraints.md](constraints.md).

## Outcome

`backend/reports.py` turns one evidence hit into an English incident report, a formal German translation, and a ticket payload. Optional speech transcription is last and is skipped unless Stage 0 marked audio in scope and every other function in this stage already works.

## Functions

```python
def incident_report(hit: EvidenceHit) -> IncidentReport:
    """English report that cites only the evidence on the hit."""
```

```python
def translate_report(report: IncidentReport, target: str = "de-DE") -> IncidentReport:
    """German report. Segment ids and numbers stay unchanged."""
```

```python
def ticket_payload(hit: EvidenceHit, report: IncidentReport) -> Ticket:
    """JSON ticket. POST only when TICKET_WEBHOOK_URL is set."""
```

## English report

Sections, in this order:

1. **Summary** — two sentences.
2. **What the footage shows** — only description, detections, and time range present on the hit.
3. **Severity and why** — the numeric severity and the rationale.
4. **Location and camera** — `location` and `camera_id`.
5. **Time window** — start and end, plus the segment id.
6. **Privacy review items** — `privacy_flags` and the fixed GDPR note.
7. **Recommended actions** — the ranking field `recommended_action`.

The report must cite the segment id and the time range in the body, not only in metadata. It must not add vehicles, people, contact, or causes that are absent from the hit. If the rationale says the evidence is weak, the summary says that too.

## German report

Translate through the same W&B model used for ranking.

- Formal **Sie**.
- Metric units.
- 24-hour time.
- Dates as `DD.MM.YYYY` when a calendar date is present. Do not invent a date from a clip offset.
- Segment ids, camera ids, scores, and severities are copied unchanged, not translated or reformatted.
- Append this exact line: `Zur manuellen Prüfung; keine Rechtsberatung.`

If the model is unavailable, this function is the third scope cut. Keep the English report and return a structured error from the German endpoint rather than a partial translation presented as complete.

## Ticket

```json
{
  "title": "short incident title",
  "severity": 4,
  "camera_id": "...",
  "location": "...",
  "time_window": {"start": 0, "end": 0, "segment_id": "..."},
  "summary": "the English summary",
  "clip_urls": {"previous": null, "event": "https://...", "next": null},
  "privacy_flags": ["people visible"],
  "assignee": null,
  "created_at": "ISO-8601"
}
```

`assignee` is always JSON `null` until a person assigns it.

When `TICKET_WEBHOOK_URL` is set, POST the payload with a timeout. A non-2xx response or a connection error raises an exception the API will turn into `{error, hint}`. Never catch and ignore a webhook failure.

When the variable is unset, return the payload for download and do not open a network connection.

## Audio

Only if Stage 0 recorded that sampled clips have speech, and only after the three functions above pass their smoke test: offer an optional Canary-1B transcript for the event segment, attached as `transcript` and omitted from the report unless present. If Stage 0 marked audio out of scope, do not call `CANARY_1B_URL`.

## Tests

`tests/test_reports.py`, no network and no LLM:

- an English report builder includes the segment id and both timestamps
- a formatting helper leaves segment ids and integers unchanged inside German text
- the required German disclaimer is present exactly once
- `ticket_payload` sets `assignee` to null and copies clip URLs, using null for a missing neighbour
- webhook posting is not attempted when the URL is empty

## Verification

`scripts/smoke_reports.py` takes one real evidence hit, prints the English report, then the German report.

Pass condition: both documents contain the same segment id and the same start and end values, and the German text includes `Zur manuellen Prüfung; keine Rechtsberatung.`

## Done when

- The smoke script has printed both reports for a real hit.
- A ticket payload is printed locally. It is posted only when the webhook variable exists, and the script reports which of the two happened.
- `PROGRESS.md` records whether German and the webhook are live or cut.
