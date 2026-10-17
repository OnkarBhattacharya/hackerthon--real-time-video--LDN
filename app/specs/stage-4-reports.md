# Stage 4 — Reports, German version, ticket (spec)

**Time-box:** 30 minutes  
**Depends on:** Stage 3 `EvidenceBundle`  
**Goal:** Turn one evidence hit into an English incident report, a formal German translation, and a ticket payload.

## In scope

| Deliverable | Description |
|-------------|-------------|
| `backend/reports.py` | `incident_report`, `translate_report`, `ticket_payload` |
| `backend/config.py` | Optional `TICKET_WEBHOOK_URL` (empty means do not call the network) |
| `scripts/smoke_reports.py` | One real hit: print English and German, check the ticket keys |
| `tests/test_reports.py` | Pure logic. No network and no live LLM |

## Out of scope

- Canary / audio transcription. Stage 0 did not confirm speech. Do not call `CANARY_1B_URL`.
- API routes and the UI (Stages 5–6)
- Object crops (already omitted in Stage 3)

The merged hit type is `EvidenceHit`. Reports cite that object only. The GDPR sentence lives on `privacy_note`; `privacy_flags` lists people and plates.

## English report

`incident_report(hit)` builds the report locally from the hit. It does not call the LLM and does not add people, vehicles, contact, or causes that are absent from the description, detections, rationale, or flags.

Sections, in order:

1. **Summary** — two sentences. Cites segment id and time range. If the rationale says the evidence is weak, the summary says that too.
2. **What the footage shows** — description, detection class counts, and the time range.
3. **Severity and why** — numeric severity and the rationale.
4. **Location and camera** — `location` and `camera_id`.
5. **Time window** — start, end, and segment id. Offsets stay in seconds. No calendar date is invented from a clip offset.
6. **Privacy review items** — `privacy_flags`, always including `Review before sharing. Possible personal data (GDPR).`
7. **Recommended actions** — the ranking field `recommended_action` only.

Clip URLs are not copied into the report text.

## German report

`translate_report(report, target="de-DE")` translates the prose with the same `LlmClient` used for ranking (`openai/gpt-oss-20b`, JSON mode, timeout).

- Formal **Sie**.
- Metric units. No invented conversion when the source has no unit.
- 24-hour time. No invented clock time.
- Dates as `DD.MM.YYYY` only when a calendar date is already present.
- Segment ids, camera ids, scores, and severities are shielded and copied back unchanged.
- The time window and the location line are written in German from the original values so the segment id and offsets cannot drift.
- The exact line `Zur manuellen Prüfung; keine Rechtsberatung.` is appended once.

If the model is unavailable or the result is still English, the function raises `ReportError`. It does not return a partial translation as a finished German report.

## Ticket

`ticket_payload(hit, report)` returns this JSON object:

| Field | Source |
|-------|--------|
| `title` | Severity and camera id |
| `severity` | Ranked severity |
| `camera_id`, `location` | Candidate |
| `time_window` | `start`, `end`, `segment_id` |
| `summary` | English summary |
| `clip_urls` | `previous`, `event`, `next`; missing neighbour is JSON `null` |
| `privacy_flags` | Copied from the hit |
| `assignee` | Always JSON `null` |
| `created_at` | UTC ISO-8601 |

When `TICKET_WEBHOOK_URL` is set, POST the payload with the shared request timeout. Log the HTTP status only. A connection error or a non-2xx response raises `TicketWebhookError` with the status or error type and without the URL. When the variable is unset, return the JSON and do not open a connection. Do not invent a webhook URL.

## Verify

```bash
python scripts/smoke_reports.py
pytest tests/test_reports.py
```

The smoke script uses the Stage 3 camera subset, ranks a small candidate sample, and assembles one hit. It prints both reports. Pass means both are non-empty, both contain the segment id and the same start and end, the German disclaimer is present once, and the ticket has the fields above with `assignee` null. The payload is posted only when the webhook variable is set. The script says which of the two happened. Clip URLs are not printed.
