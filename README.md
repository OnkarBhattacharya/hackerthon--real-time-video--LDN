# Near-miss investigation

Searches the team's indexed VAST VSS archive for close approaches between people and moving vehicles, ranks the hits, shows Before / Event / After clips, and produces an English report, a German report, and a downloadable ticket payload.

This app does not ingest video or run detectors. The running VSS stack already owns search, segments, and clips.

## How it works

1. **Investigate** starts a background job.
2. The job searches each requested camera (default: every indexed camera) with near-miss style queries.
3. Candidates are de-duplicated and severity-ranked with W&B inference when `WANDB_API_KEY` is set (otherwise a rule-based fallback).
4. Evidence assembly attaches neighbour clips and privacy flags.
5. The page groups hits by camera; each card can open reports or a ticket JSON.

## Architecture

```text
Browser (frontend/, relative URLs under /app/guest/)
   -> FastAPI near-miss-app (routes at / and /api/…)
        investigate job -> vss.py (login, search, explore, stream)
                        -> ranking.py (W&B JSON, or rules)
                        -> evidence.py (neighbour clips, privacy flags)
        reports.py (English, German, ticket payload)
VAST VSS (in-cluster http://video-backend-service:8000)
Workshop App button -> hackathon-app launcher -> GUEST_UPSTREAM=near-miss-app
```

## How to open (workshop)

1. Go to [https://workshop.thecosmoslabs.com](https://workshop.thecosmoslabs.com).
2. Click **App**.
3. Open the **second card** (`/app/guest`) — that is this investigation UI (not Idea Upscaler).

Cluster check (operators): `http://video-lab-team-41.cosmos.vastdata.com/app/guest/health` should return `"service":"near-miss-investigation"`.

## Run locally

```bash
pip install -r requirements.txt
export VSS_URL=http://video-backend-service:8000   # or $INGRESS_URL off-cluster
export VSS_USERNAME="$USERNAME"
export VSS_PASSWORD="$PASSWORD"
# Optional: WANDB_API_KEY, TICKET_WEBHOOK_URL
PORT=8080 python -m backend.main
```

Without archive credentials:

```bash
VSS_USE_FIXTURE=1 PORT=8080 python -m backend.main
```

Open `/`. Routes are unprefixed so a reverse proxy can strip `/app` or `/guest`.

```bash
python -m pytest
```

## Deploy (team-41, guest card)

No Docker build. Code is a ConfigMap tarball on `python:3.12-slim`.

- Deployment / Service: `near-miss-app`
- ConfigMap: `near-miss-app-code` (does **not** touch `hackathon-app-code`)
- Secret: `near-miss-app-vss-creds` (`VSS_URL=http://video-backend-service:8000`, username/password, optional `WANDB_API_KEY`)
- No extra Ingress — claim the launcher guest card:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 set env deploy/hackathon-app GUEST_UPSTREAM=http://near-miss-app:80
```

## Limits and known gaps

- **Crop** unavailable (no verified frame/box path).
- **Audio / Canary** out of scope.
- **Ticket webhook** unset → download-only JSON (`posted: false`).
- Neighbour clips omitted and labelled when the archive has no previous/next segment.
- English incident text is always assembled from templates; German uses the model when W&B works, otherwise a template with the formal disclaimer.
- Ranking uses W&B when the key is present; otherwise rule-based scores are labelled in the UI.
- Report/ticket APIs use `?segment_id=` query params so `s3://` ids survive the guest proxy.
- `tools/video-ask/` remains deployed as `ldn-video-ask` but is not the guest card while `GUEST_UPSTREAM` points here.
