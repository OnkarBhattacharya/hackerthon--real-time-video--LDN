# Progress

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
