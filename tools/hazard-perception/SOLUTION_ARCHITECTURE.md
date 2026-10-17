# Hazard perception — solution architecture

Second Team 41 product on the shared lab **App** button. Card 1 is Video Recutter (`tools/idea-upscaler/SEED.md`). This document is card 3.

The test plays one indexed clip, records when the tester marks a hazard, then boxes one object from the YOLO sidecar and scores the reaction. It does not generate video and it does not call Cosmos Reason to judge the answer.

## Purpose

A short hazard-perception test over the archive that is already indexed for Team 41.

Three modes pick the camera world:

| Mode | What the tester is | Clip the search keeps |
|------|--------------------|------------------------|
| Driving | A driver in street traffic | `capture_type` streets, then traffic. Warehouse rows and `nyc_bike_gopro-1` are dropped. |
| Forklift driver | A driver in a warehouse aisle | `location` `warehouse3`, `camera_id` `sdg_warehouse_cam-2`, `capture_type` `warehouse` |
| Warehouse walk | A person on foot in that aisle | The same warehouse filter |

The first pass only plays the clip. Detections are read on the second pass. One hazard is boxed. The score is a local table, 0 to 5, for Slow down, Brake, Wait, Steer away, or Continue.

## User flow

Open the workshop lab page and click **App**. The launcher is `/app/`. The third card, **Hazard perception**, opens `/app/hazard/`.

1. Pick Driving, Forklift driver, or Warehouse walk. The page `POST`s `api/clip` and sets the `<video>` to the returned `stream` URL.
2. **Start the clip** (click or Space) runs a short countdown, then pass 1. Space, a click on the picture, or **I see a hazard** stores a timestamp and paints that mark. The page also `GET`s `api/prime` so the sidecar can load while the clip plays. During either pass, dragging the timeline or pressing Left/Right scrubs the clip. A scrub that lands on the end does not open the second pass.
3. Enter, or the clip ending on its own, `POST`s `api/hazard` with the clip id and the click times. Pass 2 replays the clip with the tester's marks and one yellow box. The timeline shows those marks and the hazard time.
4. Keys 1–5 or the choice buttons pick the reaction. Enter or a second click on the choice `POST`s `api/review`. The page shows `points` out of 5 and one sentence.
5. **Another clip** repeats the same mode. **All apps** returns to the launcher.

A refresh loses the clip id. Ids live in the hazard process, and a new process does not know them.

## Components

```
Browser (workshop App button)
   ↓  /app/hazard/…   Ingress rewrite → hackathon-app /hazard/…
hackathon-app (python:3.12-slim, ConfigMap hackathon-app-code)
   ↓  HAZARD_UPSTREAM  (http://hazard-perception:80)
hazard-perception (python:3.12-slim, ConfigMap hazard-perception-code)
   ↓  login, search, detections, stream
video-backend-service:8000
   ↓
Team 41 VSS index (search rows + YOLO sidecars)
```

| Piece | Where it lives |
|-------|----------------|
| Launcher card | `tools/idea-upscaler/apps.json` id `hazard`, title fixed in `_catalog()` |
| Public proxy | `tools/idea-upscaler/main.py` route `/hazard` |
| Test page and API | `tools/hazard-perception/main.py` (HTML is the `PAGE` string; `<base href="/app/hazard/"/>`) |
| Dependencies | `tools/hazard-perception/requirements.txt` (`flask`, `requests`) |
| Clip and sidecar cache | In-process dicts on the hazard pod. One replica. |

`hackathon-app` forwards `/hazard/<subpath>` to `{HAZARD_UPSTREAM}/{subpath}`. Video responses and HTTP 206 are streamed. Any other proxy failure is plain text `Hazard perception is not reachable` with status 502.

## Request path

Ingress host `video-lab-team-41.cosmos.vastdata.com`, path `/app(/|$)(.*)`, rewrite `/$2`, backend Service `hackathon-app` port 80. The browser's `/app/hazard/api/clip` arrives at `hackathon-app` as `/hazard/api/clip` and at the hazard container as `/api/clip`.

The hazard Service is ClusterIP port 80, target 8080. It has no Ingress of its own.

| Browser | Hazard container | What it does |
|---------|------------------|--------------|
| `GET /app/hazard/` | `GET /` | The test page |
| `GET /app/hazard/health` | `GET /health` | `{"status":"ok","app":"hazard-perception"}` |
| `POST /app/hazard/api/clip` | `POST /api/clip` | Search one mode, store the clip, return id, place, duration, stream |
| `GET /app/hazard/api/stream?source=s3://…` | `GET /api/stream` | Proxy `GET /api/v1/videos/stream` with the JWT in `?token=` and the browser `Range` |
| `GET /app/hazard/api/prime?id=` | `GET /api/prime` | Load the sidecar into the cache. The page ignores a failure. |
| `POST /app/hazard/api/hazard` | `POST /api/hazard` | Pick one hazard from the sidecar and the click times |
| `POST /app/hazard/api/review` | `POST /api/review` | Score the chosen action against that hazard |

`GET /app/health` is the Video Recutter process (`app` `idea-upscaler`), not this test.

`POST /api/clip` body is `{"mode":"driving"|"forklift"|"walk"}`. Unknown modes are 400. An empty index for that mode is 503.

`POST /api/hazard` and `POST /api/review` send `id`, `clicks` (times, or `{t, x, y}`), and `duration`. An unknown id is 404. A detections error is 503. A review action outside `slow`, `brake`, `wait`, `steer`, `continue` is 400.

The public clip JSON does not include the `s3://` source. The page only receives `stream`, which is `api/stream?source=` plus the encoded URI.

## Data and search dependencies

Auth is `POST /api/v1/auth/login` with `VSS_USERNAME` and `VSS_PASSWORD`. The JWT is cached for about 25 minutes. A 401 logs in once more. `_resolve_vss_url()` rewrites a `*.cosmos.vastdata.com` or `video-lab-team-*` host to `http://video-backend-service:8000`, because the pod cannot use the public Ingress name.

Search is `POST /api/v1/search` (skill `retrieval/search`). Each mode has one query line:

| Mode | Query |
|------|--------|
| Driving | `person or cyclist stepping out near cars in street traffic` |
| Forklift driver | `forklift driver approaching a person or a rack in the aisle` |
| Warehouse walk | `person on foot with a load while a forklift comes toward them` |

Body fields that matter: `top_k` 8, `llm_top_n` 1, `min_similarity` 0.15, `include_public` true, `time_filter` `all`, and `metadata_filters` from the mode. `llm_top_n` stays at least 1 because `0` is a 422. The page does not show `llm_synthesis`.

The picker walks `chunk_results[]`, keeps rows whose `preview_source` starts with `s3://` and which `_allows()` accepts, and ranks them with the similarity score plus the chunk's `reasoning_content` and the matching `results[]` object classes. The previous clip for that mode is penalized so the next draw prefers a different segment. Driving tries `capture_type: streets`, then `capture_type: traffic`. Warehouse modes use the single warehouse filter above. Duration comes from the segment row, otherwise from the chunk's best-match window, otherwise 5 seconds, and is never below 1.5.

Detections are `GET /api/v1/videos/detections?source=` (skill `retrieval/videos`). HTTP 404 means there is no sidecar. Boxes under confidence 0.45, or tiny boxes, are ignored. Distant cars, traffic lights, benches, and clocks never qualify.

One winner is chosen. A click in the first second, or in the first fifth of the clip, or no click, searches the whole clip. A later click searches about 0.75s before it through 1.25s after it, and falls back to the whole clip if that window is empty. Priority is a person in the path, then a bicycle or motorcycle, then a close vehicle. Forklift mode can also pick a rack (`refrigerator` or `dining table` in the path) as "rack ahead". Walk mode labels a close vehicle "forklift" and a person "person with a load". The second pass follows that one object and draws a single box per frame.

The score table is `RULES` in `main.py`. The comment in that file is the contract: Cosmos Reason is not called live. A missing sidecar tells the tester to slow down, or to wait in Warehouse walk. A sidecar with no qualifying object tells them to slow down and look again. `continue` scores 0 when a labeled hazard was boxed.

This process does not write VastDB, does not ingest, and does not call `/api/v1/agent/*`.

## What this solution does not own

Video Recutter stays on `/app/upscaler`. Its cut, voice-over, and brief-filling code stay in `tools/idea-upscaler/main.py`.

Card 2 stays on `GUEST_UPSTREAM`. The launcher title is LDN Video Ask. `hackathon-app` proxies `/guest` to that variable when it is set. `tools/idea-upscaler/deploy.sh` does not set, unset, or restore it. On the running Deployment the variable is `http://near-miss-app:80`. Namespace `team-41` also has a Deployment and Service named `ldn-video-ask`. This solution does not deploy or configure either of those. When `GUEST_UPSTREAM` is empty, `hackathon-app` mounts the bundled `video_ask_app.py` under `/guest` instead of proxying.

The VSS stack (detector, reasoner, embedder, writer, `video-backend`) is already deployed. This app only reads it.

## How it is deployed

Namespace `team-41`. Kubeconfig `/config/team-41-k8s.yaml`. Humans open the workshop **App** button. The live path is `/app`, and this test is `/app/hazard/`.

Two Deployments:

| Deployment | Image | Code | Credentials | Listens |
|------------|-------|------|-------------|---------|
| `hackathon-app` | `python:3.12-slim` | ConfigMap `hackathon-app-code` at `/code` | Secret `hackathon-app-vss-creds` | 8080, Service port 80, Ingress `/app` |
| `hazard-perception` | `python:3.12-slim` | ConfigMap `hazard-perception-code` (`main.py`, `requirements.txt`) at `/code` | Secret `hazard-perception-vss-creds` | 8080, Service port 80, no Ingress |

Secret keys are `VSS_URL`, `VSS_USERNAME`, and `VSS_PASSWORD`. Values stay in the cluster. The hazard container runs `pip install -r requirements.txt` and `python main.py`. It does not install ffmpeg.

`HAZARD_UPSTREAM` on `hackathon-app` is `http://hazard-perception:80`. The code default is the same URL.

`tools/idea-upscaler/deploy.sh` refreshes only the launcher and the proxy. It applies ConfigMap `hackathon-app-code` from `tools/idea-upscaler`, restarts `deploy/hackathon-app`, and waits until `/app/upscaler` returns 200. It does not roll `hazard-perception`, and a green run of that script does not prove `/app/hazard/` is up.

Hazard code changes are a separate ConfigMap apply, then a restart:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 create configmap hazard-perception-code \
  --from-file=tools/hazard-perception \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n team-41 rollout restart deploy/hazard-perception
kubectl -n team-41 rollout status deploy/hazard-perception
```

ConfigMap updates do not appear in a running pod until the pod is recreated.

Checks from inside the cluster network:

```bash
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/hazard/health
curl -sS -o /dev/null -w '%{http_code}\n' http://video-lab-team-41.cosmos.vastdata.com/app/hazard/
```

Expect `app` `hazard-perception` and HTTP 200. Do not send people to the Ingress host as the product URL. Do not redeploy the VSS stack for this test.

## Files

| File | Role |
|------|------|
| `tools/hazard-perception/main.py` | Flask test: search, stream proxy, one-box hazard, score table, page |
| `tools/hazard-perception/requirements.txt` | `flask`, `requests` |
| `tools/hazard-perception/SOLUTION_ARCHITECTURE.md` | This note |
| `tools/idea-upscaler/main.py` | Launcher and `/hazard` proxy |
| `tools/idea-upscaler/apps.json` | Card copy |
| `tools/idea-upscaler/deploy.sh` | Refreshes `hackathon-app` only |

## Environment

| Variable | Process | Used for |
|----------|---------|----------|
| `PORT` | Both | Bind address. Deployments set `8080`. |
| `VSS_URL` / `VSS_INTERNAL_URL` | hazard-perception | Retrieval API. Public lab hosts are rewritten to `http://video-backend-service:8000`. |
| `VSS_USERNAME` / `VSS_PASSWORD` | hazard-perception | `POST /api/v1/auth/login` |
| `HAZARD_UPSTREAM` | hackathon-app | Base URL of this Service. Default `http://hazard-perception:80`. |
| `GUEST_UPSTREAM` | hackathon-app | Card 2. Left untouched by this solution and by `deploy.sh`. |
