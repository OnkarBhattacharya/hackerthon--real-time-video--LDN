# Submission — Near-miss investigation

## Problem

Safety teams need a fast way to find and explain close approaches between people and moving vehicles (including forklifts) in already-indexed workshop footage. Manual scrubbing of every camera is too slow for a demo window.

## Data used

Live VAST VSS archive for **team-41** (`http://video-backend-service:8000` in-cluster). Health reported **612** indexed segments.

Cameras exercised in the deployed investigation (guest URL), among others:

- `sdg_warehouse_cam-2` (warehouse / forklift near-miss hits, including severity 5)
- `pie_cam-3`
- `sf_streets_cam-5`
- Earlier full-archive run also searched `i24_cam-1`, `neighborhood_cam-1`, `nyc_bike_gopro-1`, `nyc_streets_cam-*`, `sf_streets_cam-*`, `smartspace_cam-1`, and returned hits from `pie_cam-3`, `sdg_warehouse_cam-2`, `sf_streets_cam-2`, `sf_streets_cam-5`

Packs are whatever the archive already indexed for those cameras (warehouse, street, PIE, etc.). This app does not ingest new video.

## How search is used

`backend/vss.py` logs in, lists camera ids, and runs near-miss queries per camera (`person close to a moving vehicle` and related defaults). Results are normalised to candidates (segment id, camera, times, description, detections, relative `api/stream` clip URL). Neighbours come from segment tools / explore when available; missing neighbours are flagged for the UI.

## How the LLM is used

`backend/ranking.py` calls W&B OpenAI-compatible inference with a strict JSON severity rubric (1–5) when `WANDB_API_KEY` is set. Deployed runs used the model for ranking (`rule_based: false`) and for German report text when the call succeeds. English incident reports are assembled from evidence templates. If W&B fails, ranking and German fall back to rules/templates and say so.

## Real vs stubbed

| Piece | Status |
| --- | --- |
| VSS login / search / stream | **Real** (in-cluster) |
| Severity ranking | **Real** W&B on deploy (rule fallback available) |
| Neighbour / event clips | **Real** proxied MP4 via `api/stream` |
| Privacy flags | **Real** from ranking + fixed GDPR note |
| English report | **Template** from evidence (cites segment id and time) |
| German report | **Model** when W&B works; else template + disclaimer |
| Ticket | **Download JSON** (`posted: false`; webhook unset) |
| Object crop | **Stubbed / unavailable** |
| Audio / Canary transcript | **Out of scope** |
| Fixture mode | Local/dev only (`VSS_USE_FIXTURE=1`); not used on the guest deploy |

## How to open

Workshop → **App** → **second card** (`/app/guest`).  
Health: `…/app/guest/health` → `"service":"near-miss-investigation"`.

Kubernetes: Deployment/Service `near-miss-app` in `team-41`; `GUEST_UPSTREAM=http://near-miss-app:80` on `hackathon-app`. No second Ingress.

## Repository

https://github.com/OnkarBhattacharya/hackerthon--real-time-video--LDN
