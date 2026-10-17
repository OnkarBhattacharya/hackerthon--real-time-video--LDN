# Progress

## Stage 1 — Data layer

- Spec: `app/specs/stage-1-data-layer.md`
- Implemented: `backend/config.py`, `models.py`, `vss.py`, `candidates.py`, `main.py` (health stub)
- Verify: `python scripts/smoke_candidates.py` (gate: ≥3 cameras with hits)
- Status: **done** — smoke passed (≥3 cameras with hits)

## Stage 2 — Severity ranking

- Spec: `app/specs/stage-2-severity-ranking.md`
- Implemented: `backend/llm.py`, `backend/ranking.py`, `RankedHit`, W&B config
- Verify: `python scripts/smoke_ranking.py`
- Status: **done** — smoke_ranking PASS (LLM JSON scores, severity sort)

## Stage 3 — Evidence assembly

- Spec: `app/specs/stage-3-evidence.md`
- Implemented: `backend/evidence.py` (triptych, privacy flags, in-memory run + `data/last_run.json`)
- Crop: **omitted** — detections have boxes only; no downloadable frame image (`crop_available=false`)
- Verify: `python scripts/smoke_evidence.py`
- Status: **done** — smoke PASS (8 ranked, top 3 checked). pie_cam-3 sev 3 before/event/after HTTP 200 MP4; two sdg_warehouse_cam-2 sev 1 hits before+event 200, after missing (last segment). Stream `Content-Type` is `binary/octet-stream` with an MP4 body. Also fixed search `object_counts` JSON-string parsing so class names are not split into letters. `data/` already gitignored.

## Stage 4 — Reports, German version, ticket

- Spec: `app/specs/stage-4-reports.md`
- Implemented: `backend/reports.py` (`incident_report`, `translate_report`, `ticket_payload`); optional `TICKET_WEBHOOK_URL` on `Settings`
- English report is built from the evidence hit only. German uses the same W&B model as ranking. Segment ids and offsets are copied unchanged. Disclaimer: `Zur manuellen Prüfung; keine Rechtsberatung.`
- Audio / Canary: **skipped** — Stage 0 did not confirm speech
- Webhook: **not configured** — payload is returned and not posted. POST runs only when `TICKET_WEBHOOK_URL` is set
- Verify: `pytest tests/test_reports.py` (9 passed); `python scripts/smoke_reports.py`
- Status: **done** — smoke PASS. One real hit, `sdg_warehouse_cam-2`, severity 1, window 5 s–10 s (sample of 4 candidates; none reached severity ≥ 3, so the keep rule returned the top of that sample). English and German both cite the segment id. German disclaimer present once. Ticket keys present, `assignee` null, next clip null. Webhook skipped.
