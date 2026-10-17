# LDN Video Ask

Hackathon app for the **VAST Builders Challenge**: ask the team’s indexed video archive in plain language, get a grounded answer, and play the evidence clips.

Built on the **pre-running VSS stack** (skills: `retrieval/login`, `retrieval/agent-qa`, `retrieval/videos`, `retrieval/suggest-prompts`). No custom FFmpeg / YOLO / local ingest pipeline.

## What it does

1. Logs into the team VSS backend with env credentials
2. Calls `POST /api/v1/agent/search-and-answer` for Q&A + chunk evidence
3. Streams matching segments via `GET /api/v1/videos/stream` (proxied)
4. Seeds the UI with `GET /api/v1/suggestions` and browse via `GET /api/v1/videos/explore`

## App code

```text
tools/video-ask/
├── main.py          # Flask UI + /api/ask|suggestions|explore|stream
├── vss_client.py    # JWT login + VSS calls
├── index.html
├── app.js
├── style.css
└── requirements.txt
```

## Deploy (guest card slot)

Ship as its **own** Deployment + ClusterIP Service (`ldn-video-ask`). Do **not** overwrite `hackathon-app-code` / launcher `main.py`, and do **not** add another Ingress.

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
APP_DIR=tools/video-ask
APP_NAME=ldn-video-ask
NS=team-41

kubectl -n "$NS" create configmap "${APP_NAME}-code" \
  --from-file="$APP_DIR" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create secret generic "${APP_NAME}-vss-creds" \
  --from-literal=VSS_URL="http://video-backend-service:8000" \
  --from-literal=VSS_USERNAME="$USERNAME" \
  --from-literal=VSS_PASSWORD="$PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -
```

Apply Deployment + Service (image `python:3.12-slim`, routes at `/` and `/health`). Then claim the launcher’s second card:

```bash
kubectl -n team-41 set env deploy/hackathon-app \
  GUEST_UPSTREAM=http://ldn-video-ask:80
```

**Open:** [https://workshop.thecosmoslabs.com](https://workshop.thecosmoslabs.com) → **App** → second card (`/app/guest`). Hard-refresh after claiming.

Give the card back: `kubectl -n team-41 set env deploy/hackathon-app GUEST_UPSTREAM-`

## Local smoke (optional)

```bash
cd tools/video-ask
pip install -r requirements.txt
# uses INGRESS_URL / USERNAME / PASSWORD already in the VM env
PORT=8080 python main.py
```

## Out of scope (by design)

- Rebuilding ingest / embed / YOLO locally
- Internet / YouTube video upload
- Heavy Node frontend (ConfigMap size limit ~1 MiB)
