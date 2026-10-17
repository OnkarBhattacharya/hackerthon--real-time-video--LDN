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

The first pass plays the clip and can prime the sidecar. The hazard is chosen when the second pass starts. One hazard is boxed. The score is a local table, 0 to 5, for Slow down, Brake, Wait, Steer away, or Continue.

## User flow

Open the workshop lab page and click **App**. The launcher is `/app/`. The third card, **Hazard perception**, opens `/app/hazard/`.

1. Pick Driving, Forklift driver, or Warehouse walk. The page `POST`s `api/clip` with `mode` and `seen`, then sets the `<video>` to the returned `stream` URL.
2. **Start the clip** (click or Space) runs a 3-second countdown, then pass 1. Space, a click on the picture, or **I see a hazard** stores `{t, x, y}` and paints a numbered mark. At most 12 marks. The page also `GET`s `api/prime` so the sidecar can load while the clip plays. During either pass, dragging the timeline or pressing Left/Right scrubs. `seekTo` holds a guard for 800ms, and an `ended` event during that guard does not open the second pass, including a scrub that lands on the end.
3. Enter, or the clip ending on its own, `POST`s `api/hazard` with the clip id, the clicks, and the duration. Pass 2 replays the clip with the tester's marks and one box (`#e7ff6a`) on the winning object. The timeline shows the click marks and a window from 0.75s before the hazard time to 1.25s after it.
4. Keys 1–5 select the reaction. Enter submits it. A click on a choice button selects and submits in the same click. That `POST`s `api/review`. The page shows `points` out of 5 and one sentence, and adds the points to a session total.
5. **Different clip** repeats the same mode and sends the sources already played. **Change mode** returns to the picker. **Done**, once at least one clip is scored, opens “Congratulations you scored E/N”, where E is the sum of the points and N is 5 times the number of scored clips, then plays a short confetti animation. **Change mode** on that screen returns to the picker. **All apps** returns to the launcher.

A refresh loses the clip id and the session total. Ids live in the hazard process, and a new process does not know them. Sources already played stay in `sessionStorage` under `hazard-perception-seen`, so the next clip still avoids them.

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
| Clip and sidecar cache | In-process dicts. A second replica would not share them. |

`hackathon-app` forwards `/hazard/<subpath>` to `{HAZARD_UPSTREAM}/{subpath}`. Video responses and HTTP 206 are streamed. Any other proxy failure is plain text `Hazard perception is not reachable` with status 502.

## Request path

Ingress host `video-lab-team-41.cosmos.vastdata.com`, path `/app(/|$)(.*)`, rewrite `/$2`, backend Service `hackathon-app` port 80. The browser's `/app/hazard/api/clip` arrives at `hackathon-app` as `/hazard/api/clip` and at the hazard container as `/api/clip`.

The hazard Service is ClusterIP port 80, target 8080. It has no Ingress of its own.

| Browser | Hazard container | What it does |
|---------|------------------|--------------|
| `GET /app/hazard/` | `GET /` | The test page |
| `GET /app/hazard/health` | `GET /health` | `{"status":"ok","app":"hazard-perception"}` |
| `GET` or `POST /app/hazard/api/clip` | `GET` or `POST /api/clip` | Search one mode, store the clip, return id, place, duration, stream, and the s3 URIs |
| `GET /app/hazard/api/stream?source=s3://…` | `GET /api/stream` | Proxy `GET /api/v1/videos/stream` with the JWT in `?token=` and the browser `Range` |
| `GET /app/hazard/api/prime?id=` | `GET /api/prime` | Load the sidecar into the cache. The page ignores a failure. |
| `POST /app/hazard/api/hazard` | `POST /api/hazard` | Pick one hazard from the sidecar and the click times |
| `POST /app/hazard/api/review` | `POST /api/review` | Score the chosen action against that hazard |

`GET /app/health` is the Video Recutter process (`app` `idea-upscaler`), not this test.

`POST /api/clip` body is `{"mode":"driving"|"forklift"|"walk","seen":["s3://…"]}`. `GET /api/clip?mode=` works the same, but `seen` is read only from a JSON body. Unknown modes are 400. An empty index, a mode with only one eligible clip, or a session that has already played every eligible clip is 503.

`POST /api/hazard` and `POST /api/review` send `id`, `clicks` (times, or `{t, x, y}`), and `duration`. The server keeps `t` and ignores `x` and `y`. An unknown id is 404. A detections error is 503. A review action outside `slow`, `brake`, `wait`, `steer`, `continue` is 400.

The clip JSON includes `id`, `mode`, `mode_name`, `place`, `duration`, `stream`, `source`, and `video`. `source` and `video` are `s3://` URIs so the page can send them back as `seen`. The `<video>` element uses `stream`, which is `api/stream?source=` plus the encoded URI.

## Data and search dependencies

Auth is `POST /api/v1/auth/login` with `VSS_USERNAME` and `VSS_PASSWORD`. The JWT is cached for about 25 minutes. A 401 logs in once more. `_resolve_vss_url()` rewrites a `*.cosmos.vastdata.com` or `video-lab-team-*` host to `http://video-backend-service:8000`, because the pod cannot use the public Ingress name.

Search is `POST /api/v1/search` (skill `retrieval/search`). Each mode has one query line:

| Mode | Query |
|------|--------|
| Driving | `person or cyclist stepping out near cars in street traffic` |
| Forklift driver | `forklift driver approaching a person or a rack in the aisle` |
| Warehouse walk | `person on foot with a load while a forklift comes toward them` |

Body fields that matter: `top_k` starts at 8, `llm_top_n` 1, `min_similarity` 0.15, `include_public` true, `time_filter` `all`, and `metadata_filters` from the mode. `llm_top_n` stays at least 1 because `0` is a 422. The page does not show `llm_synthesis`.

The picker walks `chunk_results[]`, keeps rows whose `preview_source` starts with `s3://` and which `_allows()` accepts, and ranks them with the similarity score plus the chunk's `reasoning_content` and the matching `results[]` object classes. Played clips are not score-penalized. The page sends `seen` (at most 40 `s3://` URIs). `_best_chunk` drops those URIs and every other chunk of the same parent video (`original_video`, or the filename with `_chunk_NNNN` removed). `top_k` starts at 8 and widens to 32, then 100, while every returned hit is a video already played. One eligible parent returns “This mode only has one eligible clip in the archive.” A session that has used every eligible clip returns “Every clip for this mode was already used in this session.” Driving tries `capture_type: streets`, then `capture_type: traffic`. Warehouse modes use the single warehouse filter above. Duration comes from the segment row, otherwise from the chunk's best-match window, otherwise 5 seconds, and is never below 1.5. Clip ids look like `driving-1` and live only in this process.

Detections are `GET /api/v1/videos/detections?source=` (skill `retrieval/videos`). HTTP 404 means there is no sidecar. Boxes under confidence 0.45, or tiny boxes, are ignored. Distant cars, traffic lights, benches, and clocks never qualify.

One winner is chosen. A click in the first second, or in the first fifth of the clip, or no click, searches the whole clip. A later click searches about 0.75s before it through 1.25s after it, and falls back to the whole clip if that window is empty. Priority is a person in the path, then a bicycle or motorcycle, then a close vehicle. Forklift mode can also pick a rack (`refrigerator` or `dining table` in the path) as "rack ahead". Walk mode labels a person "person with a load", a close vehicle "forklift", and a large rack "forklift" as well. The second pass follows that one object for about 1.25s around the winning time and draws a single box per frame.

The score table is `RULES` in `main.py`. The comment in that file is the contract: Cosmos Reason is not called live. A missing sidecar scores Slow down 5, Brake 4, Wait 3, Steer away 2, Continue 0, except Warehouse walk, which scores Wait 5, Steer away 4, Slow down 3, Brake 2, Continue 0. A sidecar with no qualifying object scores Slow down 5, Wait 4, Brake 3, Steer away 2, Continue 1, and the sentence says to slow down and look again. `continue` scores 0 when a labeled hazard from `RULES` was boxed.

This process does not write VastDB, does not ingest, and does not call `/api/v1/agent/*`.

## What this solution does not own

Video Recutter stays on `/app/upscaler`. The direction line, brief filling, archive search, ffmpeg cut, voiceover, upload, and Commons B-roll stay in `tools/idea-upscaler/main.py`. See `tools/idea-upscaler/SEED.md`.

Card 2 stays on `GUEST_UPSTREAM`. The launcher title is LDN Video Ask. `hackathon-app` proxies `/guest` to that variable when it is set. `tools/idea-upscaler/deploy.sh` does not set, unset, or restore it. The last recorded value on the Deployment, not re-checked for this doc pass, was `http://near-miss-app:80`. Namespace `team-41` was also recorded as having a Deployment and Service named `ldn-video-ask`. This solution does not deploy or configure either of those. When `GUEST_UPSTREAM` is empty and `video_ask_app.py` is present, `hackathon-app` mounts that app under `/guest` instead of proxying.

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

`tools/idea-upscaler/deploy.sh` refreshes only the launcher and the proxy. It server-side applies ConfigMap `hackathon-app-code` from `tools/idea-upscaler`, restarts `deploy/hackathon-app`, and waits until `/app/upscaler` returns 200. It does not roll `hazard-perception`, and a green run of that script does not prove `/app/hazard/` is up.

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
