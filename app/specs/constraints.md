# Cross-stage constraints

These rules apply to Stages 1–7. Stage 0 only records what the live environment actually returns. Do not invent endpoint shapes ahead of `NOTES.md`.

## Product

Build a near-miss investigation tool on the **already-running VAST VSS archive**. Search indexed footage, rank close approaches between people and moving vehicles, assemble playable evidence, and produce an English report, a German report, and a ticket payload.

```text
Browser (static page, relative URLs)
   -> FastAPI (/app)
        investigate job -> vss.py (login, search, videos)
                        -> ranking.py (W&B inference, strict JSON)
                        -> evidence.py (neighbour clips, crops, privacy flags)
        reports.py (English report, German translation, ticket payload)
VAST VSS archive (already running): search, segment details, clips, detections
```

## Layout

| Path | Owns |
| --- | --- |
| `backend/config.py` | Environment variables. Fail clearly when a required value is missing. |
| `backend/vss.py` | Retrieval client. |
| `backend/ranking.py` | Severity scoring. |
| `backend/evidence.py` | Triptych, optional crop, privacy flags, `data/last_run.json`. |
| `backend/reports.py` | English report, German translation, ticket payload. |
| `backend/jobs.py` | In-memory job store and investigate pipeline. |
| `backend/main.py` | FastAPI app and `/api` router, mounted so the app works behind `/app`. |
| `frontend/` | `index.html`, `app.js`, `styles.css`. No build step. No CDN. |
| `scripts/smoke_*.py` | Stage verification scripts. |
| `tests/` | Pure-logic pytest tests. |
| `NOTES.md` | Stage 0 findings. Later stages read this; they do not re-discover. |
| `PROGRESS.md` | What landed each stage and what was cut. |
| `data/last_run.json` | Last successful investigation snapshot. Gitignored. |

The existing `app/` package (local ingest, FFmpeg, YOLO, Whisper) is a different pipeline. These stages do not extend it and do not reimplement ingest, embedding, or detection.

## Code quality

- Type hints and a short docstring on every public function.
- No dead code. Files stay short enough that the project can be read in about ten minutes.
- One config module. No scattered `os.environ` reads.
- Every external call has a timeout, handles errors, and logs one line. Logs never include passwords, tokens, or webhook secrets.
- Pin dependencies in `requirements.txt`.
- Pytest covers pure logic only: candidate merge and de-duplication, JSON validation and repair, severity sorting, German formatting helpers.

## Acceptance

All of the following must pass before the project is called done:

- Stage 0 findings are in `NOTES.md`.
- Candidates come from at least three cameras.
- At least six ranked hits, each with severity, rationale, and privacy flags.
- Top hits show three playable clips, or a clear note when a neighbour is missing.
- English and German reports for a real hit cite the segment id and time range.
- A ticket payload is generated, and posted only when `TICKET_WEBHOOK_URL` is set.
- The app is deployed at `/app` on the team host and one investigation is run there.
- No secrets in the repo. `.gitignore` covers `.env`, kubeconfig, `data/`, and samples that contain credentials.
- `README.md`, `NOTES.md`, `PROGRESS.md`, and `SUBMISSION.md` exist and match the running app.
- Unit tests pass.

## Fallbacks

Apply the matching fallback instead of blocking:

| Condition | Do this |
| --- | --- |
| W&B inference is down or will not return valid JSON after one repair | Rank with rules: `search_score`, counts of persons and vehicles, and description words such as braking, swerving, close, near. Reports come from templates. Say in the UI and in `NOTES.md` that this is the fallback. |
| No frame or boxes | Skip crops. Keep clips. |
| No neighbour segment | Show the event clip only and label it. |
| Search returns nothing | Check login, then the dashboard, then `retrieval/list-metadata` values before changing queries. |
| Deploy fails | Keep the app running locally on the VM and say so. |
| A camera is not indexed | Skip it and say so in the UI. |

## Scope cuts, in this order

Cut only when the time box is gone. Never cut ranking, the triptych or event clip, privacy flags, deployment, or submission.

1. Canary-1B audio transcript.
2. Object crops.
3. German translation. Keep the English report.
4. Ticket webhook. Keep the downloadable JSON payload.
5. Custom query box. Keep the default investigation query.

## Human gates

- After Stage 0, stop and wait until the operator types `go`.
- Do not start Stage 7 submission until a deployed or explicitly local-fallback investigation has been run.
