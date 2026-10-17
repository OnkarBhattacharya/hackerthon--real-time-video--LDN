# Idea Upscaler — seed notes

Copy `tools/idea-upscaler/` when you want the same shape of app: a small Python process on the team's cluster, one lab **App** button, retrieval against the already-indexed archive, and a second button another person can claim.

This is not a video generator. It does not download footage from the internet. It searches the team's VSS index and, for an uploaded clip, cuts archive matches down to that clip's duration.

## What the running app does

The lab page has one **App** button. Ingress sends `/app` to this process and strips the prefix, so the container sees `/`.

`GET /` is a launcher with two cards:

| Card | Route the browser opens | What it is |
|------|-------------------------|------------|
| Idea Upscaler | `/app/upscaler` | This project |
| Teammate app | `/app/guest` | Open slot, or a reverse proxy if `GUEST_UPSTREAM` is set |

Idea Upscaler:

1. Reads a logline, optional beats, optional VO lines.
2. Optional: loads an upload by tag (the demo tag is `shittyvideo`).
3. Splits that into a few shots (W&B serverless chat if `WANDB_API_KEY` is set, otherwise a line split).
4. Searches `POST /api/v1/search` once per shot.
5. If the request was seeded from an upload, downloads the source clip and the best matches, then ffmpeg-cuts one mp4 of the **same duration** as the source. VO lines are burned in as subtitles, split evenly across that duration.

There is no Mars footage in this archive. An instruction like "set it on Mars" can only be satisfied by whatever real clips search returns.

## How someone else puts their app behind the second button

The cluster only exposes path `/app` for hackathon apps. Do not add another Ingress host or take path `/`.

1. Deploy their process in namespace `team-41` as a ClusterIP Service. Their container routes stay at `/`.
2. Point this process at that Service:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 set env deploy/hackathon-app \
  GUEST_UPSTREAM=http://THEIR-SERVICE:THEIR-PORT
```

3. Edit `apps.json` so the card title and `live_blurb` match their product. `href` stays `guest`.
4. Reload code:

```bash
kubectl -n team-41 create configmap hackathon-app-code \
  --from-file=tools/idea-upscaler \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n team-41 rollout restart deploy/hackathon-app
```

Until `GUEST_UPSTREAM` is set, `/app/guest` serves the bundled teammate app (`video_ask_app.py`, LDN Video Ask), mounted with Werkzeug so its `/` and `/api/ask` stay under `/guest`. After `GUEST_UPSTREAM` is set, those paths are forwarded to that Service instead. The browser never needs their in-cluster DNS name.

The ConfigMap is built with `--from-file` on this directory and is not recursive. A second app's files have to sit next to `main.py` (`video_ask_app.py`, `vss_client.py`, `index.html`, `app.js`, `style.css`), not in a subfolder. Their page builds API URLs from `window.location.pathname`, so `/app/guest/api/ask` reaches their routes.

`apps.json` fields:

| Field | Meaning |
|-------|---------|
| `id` | `upscaler` is built in. `guest` is the claimable slot. |
| `title` | Card heading. |
| `blurb` | Card text while the slot is empty. |
| `live_blurb` | Card text after `GUEST_UPSTREAM` is set. Guest only. |
| `href` | Path under `/app/`. `upscaler` or `guest`. |
| `ready` | Initial flag. Guest is forced from the env var at request time. |

## Files

| File | Role |
|------|------|
| `main.py` | Flask app: launcher, upscaler page, VSS client, ffmpeg cut, guest proxy |
| `apps.json` | Launcher cards |
| `requirements.txt` | `flask`, `requests` |
| `SEED.md` | This note |
| `TEAM_HANDOVER.md` | Short product note for Discord |
| `TEAM_HANDOVER_ENV.md` | How to get onto the VM |

Kubernetes objects (not in git): Deployment/Service/Ingress `hackathon-app`, ConfigMap `hackathon-app-code`, Secret `hackathon-app-vss-creds`. Image is `python:3.12-slim`. Code is mounted at `/code`. The container installs `ffmpeg` and `fonts-dejavu-core` before `python main.py`, because the cut needs them and the public image does not include them.

## Environment

| Variable | Where it comes from | Used for |
|----------|---------------------|----------|
| `PORT` | Deployment, `8080` | Bind address |
| `VSS_URL` / `VSS_INTERNAL_URL` | Secret | Retrieval API. Must be `http://video-backend-service:8000` inside the pod |
| `VSS_USERNAME` / `VSS_PASSWORD` | Secret, team config | `POST /api/v1/auth/login` |
| `WANDB_API_KEY` / `WANDB_TEAM` / `WANDB_PROJECT` | Secret | Optional shot-list LLM |
| `WANDB_MODEL` | Optional | Default `meta-llama/Llama-3.1-8B-Instruct` |
| `GUEST_UPSTREAM` | Deployment env | Teammate Service base URL |

If `VSS_URL` is the public host `video-lab-team-41.cosmos.vastdata.com`, `_resolve_vss_url()` rewrites it to the in-cluster Service. Pods cannot resolve that public name.

Do not print the secret, do not commit it, do not `kubectl get secret -o yaml` into chat.

## HTTP surface (what the container sees)

Ingress rewrite means the browser's `/app/health` is the container's `/health`.

| Container path | Browser path | Purpose |
|----------------|--------------|---------|
| `GET /` | `/app/` | Launcher |
| `GET /upscaler` | `/app/upscaler` | Upscaler page |
| `GET /guest` | `/app/guest` | Slot page or proxy |
| `GET /health` | `/app/health` | `{"status":"ok","app":"idea-upscaler"}` |
| `POST /api/upscale` | `/app/api/upscale` | Shot list, and a rendered cut when seeded |
| `GET /api/upload-seed?tag=` | `/app/api/upload-seed` | Preview an upload tag |
| `GET /api/stream?source=s3://…` | `/app/api/stream` | Proxied VSS segment, so `<video>` does not need the VSS JWT |
| `GET /api/cuts/<12 hex>` | `/app/api/cuts/…` | Rendered mp4 |

The page hardcodes `/app/api/...` and `<base href="/app/">`. A relative `api/upscale` from `/app/upscaler` would miss this process and hit the VSS UI HTML. That was the `Unexpected token '<'` bug.

`POST /api/upscale` body:

```json
{
  "logline": "required unless seed_from_upload",
  "beats": "optional, one beat per line",
  "transcript": "optional, one VO line per line",
  "seed_from_upload": true,
  "upload_tag": "shittyvideo"
}
```

Response fields that matter: `shots[]`, `directors_cut[]`, `source_upload`, `rendered_cut` (`url`, `target_duration_sec`, `pieces`, `vo`), `render_error`.

## Pipeline inside `main.py`

**Login.** `_login` caches the JWT for about 25 minutes. `_vss` sends `Authorization: Bearer`. On 401 it logs in once more. Playback uses `?token=` on the VSS stream URL because that is how `/videos/stream` is written. The browser token is only on our `/api/stream` proxy, not on a page the user copies around.

**Shot list.** `_wandb_shots` calls `https://api.inference.wandb.ai/v1/chat/completions` and parses a JSON object out of the model text. If the key is missing, the call fails, or the model returns fewer than 3 shots, `_heuristic_shots` splits the beats, transcript, and logline on newlines. Seeded runs keep only the first 3 shots so the cut does not wait on eight searches.

**Search.** `_search_shot` posts:

```json
{
  "query": "film-aware sentence",
  "top_k": 5,
  "llm_top_n": 1,
  "min_similarity": 0.2,
  "include_public": true,
  "time_filter": "all"
}
```

`llm_top_n` must be at least 1. `0` is a 422 and looks like an empty index. Hits are read from `results[]`, then `chunk_results[]`. Each hit's `playback_url` is `/app/api/stream?source=...`.

**Upload seed.** Tags are filters, not captions. Querying the word `shittyvideo` returns nothing. `_load_upload_seed` searches with the tag filter and visual queries (`hands holding a small toy figure`, then broader ones). If those are empty it searches the last day without the tag and keeps rows whose tags or filename contain the tag. The indexed object for the demo upload is `20261017_100245_8d0e1f47.mp4`, not the name the upload form showed.

**Cut.** `_render_same_length_cut`:

1. Download every source segment and sum `ffprobe` durations. That sum is the target length (the demo clip is about 2.37s).
2. Take up to four distinct match `source` URIs.
3. Scale each to 1280x720, 24 fps, no audio.
4. Trim or loop each piece so the pieces add up to the target.
5. Concat demuxer, then `trim=duration=<target>`.
6. If `transcript` has lines, write `vo.srt` with equal slices of the timeline and burn it with the `subtitles` filter (DejaVu Sans).

Files land in `/tmp/idea-cuts/<id>/cut.mp4`. The pod disk is ephemeral. A restart drops rendered cuts.

**Errors.** The JSON error handler exists so `fetch` never receives Flask's HTML 500 page. Render failures are returned as `render_error` with the shot list still intact.

## Deploy again

From the VM, in this repo:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 create configmap hackathon-app-code \
  --from-file=tools/idea-upscaler \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n team-41 rollout restart deploy/hackathon-app
kubectl -n team-41 rollout status deploy/hackathon-app
```

ConfigMap updates do not show up in a running pod until the pod is recreated. `rollout restart` does that. If the pod name does not change, the mount can still be the old file; delete the pod.

Open the app from the workshop lab page **App** button. Hard-refresh after a restart. Do not send people to the Ingress host as the product URL.

The Deployment's start command must keep installing ffmpeg. A fresh `python:3.12-slim` pod without that line will boot and then fail the cut with "ffmpeg not found".

## Checks

```bash
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/health
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/ | head
```

The second command should be the launcher (`Team 41`), not the upscaler form. The form is `/app/upscaler`.

Seeded upscale (slow: search plus ffmpeg):

```bash
curl -sS -m 180 -X POST http://video-lab-team-41.cosmos.vastdata.com/app/api/upscale \
  -H 'Content-Type: application/json' \
  -d '{"logline":"Edit the hand out.","transcript":"Line one.\nLine two.","seed_from_upload":true,"upload_tag":"shittyvideo"}'
```

Expect `rendered_cut.target_duration_sec` near the upload length, `vo: true`, and `GET` of `rendered_cut.url` returning `video/mp4`.

## Failures already hit

| Symptom | Cause |
|---------|--------|
| HTML where JSON was expected | Frontend called a path outside `/app`, or Flask returned an HTML traceback |
| Search always empty | `llm_top_n: 0`, or the query was the tag string |
| Login DNS failure in the pod | `VSS_URL` was the public Ingress host |
| `502` right after restart | Request arrived while the new pod was still in `apt-get` |
| Cut missing | ffmpeg not installed, or no `source` on the matches |
| Second app 404 on the public host | Their Service is only reachable as `GUEST_UPSTREAM`, via `/app/guest` |

## Archive this build was written against

Team 41, namespace `team-41`. Mostly streets, traffic, a warehouse, and an indoor crowd camera. The rough demo upload is tag `shittyvideo`: a hand moving a small white toy. Search stand-ins come from that corpus (street and bike footage), not from a generated scene.

## Using this as a seed

Keep the launcher, the `/app` prefix, the in-cluster VSS client, and the ConfigMap deploy. Replace `_wandb_shots`, `_search_shot`, and `_render_same_length_cut` if the product is different. Leave the guest card so the next person does not have to fight you for the App button.
