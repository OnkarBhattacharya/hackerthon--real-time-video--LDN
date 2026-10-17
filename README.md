# Near-miss investigation

Searches an indexed VAST video archive for close approaches between people and moving vehicles, ranks the hits, and shows the clips with an English report, a German report, and a ticket payload.

The running VSS stack does the search. This app does not ingest video or run a local detector.

## How it works

An investigation job searches each indexed camera, keeps the strongest segments, scores severity from the description and detections, then attaches the previous, event, and next clips plus privacy flags.

## Architecture

```text
Browser (frontend/, relative URLs)
   -> FastAPI (backend/main.py, also at ingress /app)
        investigate job -> vss.py (login, search, explore, stream)
                        -> ranking.py (W&B JSON, or rules if unset)
                        -> evidence.py (neighbour clips, privacy flags)
        reports.py (English report, German text, ticket payload)
VAST VSS archive: search, segment fields, clips
```

## Run locally

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
# Live archive
export VSS_URL="$INGRESS_URL"   # or the in-cluster video-backend URL
export VSS_USERNAME="$USERNAME"
export VSS_PASSWORD="$PASSWORD"
# Optional: WANDB_API_KEY, WANDB_BASE_URL, WANDB_MODEL, TICKET_WEBHOOK_URL
PORT=8080 .venv/bin/python -m backend.main
```

Without archive credentials, the same server can use the labelled sample set:

```bash
VSS_USE_FIXTURE=1 PORT=8080 .venv/bin/python -m backend.main
```

Open `/`. Behind the team ingress the same routes are served at `/app` because the app does not hard-code that prefix.

`pytest` uses the fixture and does not call the archive:

```bash
.venv/bin/python -m pytest
```

With archive credentials set, the same pipeline can be checked against the live index. The scripts do not print tokens:

```bash
python scripts/smoke_candidates.py
python scripts/smoke_ranking.py
python scripts/smoke_evidence.py
python scripts/smoke_reports.py
```

## Deploy

Not deployed from this machine. The team kubeconfig, `INGRESS_URL`, and login variables are not present here. On the team VM, follow `deployment/deploy-app-no-registry`: team namespace, no image build, ingress path `/app`. Until that is done, run the server on the VM and open the local port.

`tools/video-ask/` is the earlier ask UI and is unchanged.

## Limits

- Crop is unavailable. Cards show clips only.
- Audio transcripts are out of scope.
- With no `WANDB_API_KEY`, severity is rule-based and the German report is a template. Both say so.
- With no `TICKET_WEBHOOK_URL`, Create ticket returns JSON for download and does not POST.
- Neighbour clips are omitted, and labelled, when the archive does not return them.
- Fixture mode is sample data, not the indexed archive.
