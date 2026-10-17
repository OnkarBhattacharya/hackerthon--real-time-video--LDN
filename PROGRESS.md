# Progress

## Stages 1–4 merged into the investigation app

The Stages 1–4 branch was merged onto the investigation API. Shared modules keep the interfaces the job, fixture, and review page already call. Live retrieval uses the archive calls that branch verified: `POST /api/v1/search`, camera ids from `GET /api/v1/metadata/values`, and neighbour clips from `GET /api/v1/tools/segments` when a search row includes `segment_number`. Stored clip URLs stay relative (`api/stream?source=`) so the page plays them without a bearer token in the JSON.

That branch recorded these live checks before the merge: candidate smoke with at least three cameras, severity ranking, triptych clips with crop omitted, and English/German reports plus a ticket payload that was not posted. On this tree, `pytest` covers the fixture. The scripts under `scripts/` repeat the live checks when archive credentials are set.

## Stage 6

Click-through on `http://127.0.0.1:8090` with `VSS_USE_FIXTURE=1` (the server that was already running).

- Empty state says what Investigate does. The status line showed 7 indexed segments and the snapshot time `2026-10-17T11:29:04.892078+00:00`.
- Investigate with the default query. The button stayed disabled while the steps updated: searching `wh-cam-1`, `road-cam-2`, and `yard-cam-3`, then ranking 7 candidates and assembling evidence.
- Six hits in three groups: Warehouse aisle 2 / `wh-cam-1`, Street east / `road-cam-2`, Yard gate / `yard-cam-3`. Badges show the number and the word. Rule-based hits say the score did not come from the model. The event player has a dark outline. `seg-road-2` shows "No after segment for this video."
- Before, Event, and After reached `readyState` 4 and played. Nothing autoplays.
- English report for `seg-wh-1` downloaded as `seg-wh-1-en.txt`. The German report cites `seg-wh-1` and includes "Zur manuellen Prüfung; keine Rechtsberatung." The ticket downloaded as `seg-wh-1-ticket.json` with `posted: false`.
- A blank query sent `person close to a moving vehicle`.
- At 390px the page did not overflow. Buttons stayed 44px tall and stacked. Videos stayed inside the viewport.
- Reload showed the new last-run time `2026-10-17T11:41:08.993940+00:00`.

Fixed while matching the spec: query placeholder, blank input sends the default, API paths follow the current directory (including `/index.html` and a prefix such as `/app`), skipped cameras come from the job payload and the step text, report and ticket failures stay on the card, and the query box and buttons use a visible focus outline.

Copy in this embedded browser falls back to "Select the text to copy" because the document is not focused (`NotAllowedError`). The report text stays in the panel. A focused window uses the clipboard API.

This fixture run did not skip a camera. The page still prints a camera named in `skipped_cameras` or in a "Skipping camera …" step.

Crop and audio are off. Ranking and German text use the template fallback because `WANDB_API_KEY` is unset. Tickets are download-only.

## Stage 5–6 on `feat/stage-5-api`

The investigation API and one-page UI are implemented. Stages 1–4 are the `backend/` modules the job calls (`vss.py`, `candidates.py`, `ranking.py`, `evidence.py`, `reports.py`). They were not present when this branch started.

- `pytest`: 19 passed.
- Curl against `http://127.0.0.1:8090` with `VSS_USE_FIXTURE=1`: health ok, job `726c7d6c-83ee-432e-b618-70dec867e28c` succeeded with 6 hits from `wh-cam-1`, `road-cam-2`, and `yard-cam-3`. English report, German template, and ticket payload all returned. The sample clip returned `200 video/mp4`. An unknown job returned 404 `{error, hint}`.
- Browser: empty state, Investigate, step list, three camera groups, severity badges, triptych, missing After label on `seg-road-2`, English report, German template, ticket JSON. The event clip reached `readyState` 4 and played. Reload kept the last-run time. At 390px the page did not overflow and the action buttons stayed 44px tall.
- Copy in this embedded browser falls back to "Select the text to copy" because clipboard access is blocked. The report text stays in the panel.
- Crop and audio are off. See `NOTES.md`.
- Ranking and German text use the template fallback because `WANDB_API_KEY` is unset.
- Tickets are download-only. `TICKET_WEBHOOK_URL` is unset.
- Live VSS login was not possible in this shell. API and browser checks use `VSS_USE_FIXTURE=1`.

## Stage 7

Not deployed. This machine has no team kubeconfig and no `INGRESS_URL`. The app can be shown from a local server. `SUBMISSION.md` is not written yet.
