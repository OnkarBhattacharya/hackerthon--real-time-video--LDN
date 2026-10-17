# Video Recutter — seed notes

The folder is still `tools/idea-upscaler/`. The product on card 1 is Video Recutter. Copy this directory when you want the same shape of app: a small Python process on the team's cluster, one lab **App** button, retrieval against the already-indexed archive, and two other cards (LDN Video Ask, Hazard perception).

This is not a picture generator. A normal recut searches the team's VSS index and ffmpeg-cuts archive matches into one mp4. Two optional paths do download footage: **CC B-roll** (one Wikimedia Commons clip when a beat is missing) and the level-crossing brief when CC B-roll is ticked and the archive has no railway clip.

## What the running app does

The lab page has one **App** button. Ingress sends `/app` to this process and strips the prefix, so the container sees `/`.

`GET /` is a launcher with three cards. Titles are fixed in `_catalog()` even if `apps.json` disagrees.

| Card | Route the browser opens | What it is |
|------|-------------------------|------------|
| Video Recutter | `/app/upscaler` | This project. Page title is Video Recutter. |
| LDN Video Ask | `/app/guest` | Bundled `video_ask_app.py`, or a reverse proxy if `GUEST_UPSTREAM` is set |
| Hazard perception | `/app/hazard/` | Proxy to `HAZARD_UPSTREAM` (default `http://hazard-perception:80`). See `tools/hazard-perception/SOLUTION_ARCHITECTURE.md` |

Video Recutter, as the page works today:

1. **Direction** is one line. **Fill the brief** (`POST /api/expand`) writes the logline, storyboard beats, and spoken lines. It does not search or cut. Cosmos Reason first, then W&B. If both fail, the button returns an error. The local split is only used later, inside Recut.
2. **Recut** (`POST /api/upscale`) searches `POST /api/v1/search` and ffmpeg-cuts one mp4. Shots default to 4 (range 2–12). Cut length is Automatic unless Custom seconds (4–90) is selected, or the scenario text states a count or a length.
3. Preset chips fill the text fields only. They do not change Shots, Seconds, voice, or bed. **Streets · NYC bike** is the short demo. **My upload** checks “Use my upload”, fills a toy-clip brief, and recuts immediately.
4. **Safety · level crossing** is its own five-beat film (approach, warning, misuse, near miss, rule). The render asks for five slices (`max_pieces` is 5). A stated shot count can still change the JSON `shot_count`; it does not change that 5. With Automatic length and no length in the text, the target is 60 seconds. It searches the archive for a real railway crossing. Wikimedia Commons is used only when **CC B-roll** is ticked. A street crosswalk is not a substitute. If Cosmos Reason answers, it may reorder those railway matches and rewrite the spoken lines; a shorter list is cycled back to five slices. Typing a Direction and recutting leaves this path: a direction forces `level_crossing` off.
5. Optional **Use my upload** reads an already indexed tag (default `my-upload`) and keeps those sources out of the matches. The upload form posts the file to VSS. Indexing is not instant. The hint on the page says a minute or two.
6. Scenario lines (1–3, under Upload settings) are the VAST reasoner prompt on upload, and on Recut they are also search text. If a line states a shot count or a length, that overrides the controls. Six is not a default.

The JSON field `pitch` is still the old sentence “We start from boards and voice…”. The page does not show it. The page lead is “Type one line. Fill the brief writes the film. Recut makes the video.”

There is no Mars footage in this archive. An instruction like “set it on Mars” can only be satisfied by whatever real clips search returns. Fill-the-brief is told not to write Mars, space, fantasy, or railways.

## How someone else puts their app behind the second button

The cluster only exposes path `/app` for hackathon apps. Do not add another Ingress host or take path `/`.

1. Deploy their process in namespace `team-41` as a ClusterIP Service. Their container routes stay at `/`.
2. Point this process at that Service:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 set env deploy/hackathon-app \
  GUEST_UPSTREAM=http://THEIR-SERVICE:THEIR-PORT
```

3. Edit `apps.json` so the guest card `blurb` and `live_blurb` match their product. `_catalog()` keeps the title **LDN Video Ask** and `href` `guest`.
4. Reload code with `tools/idea-upscaler/deploy.sh` (server-side ConfigMap apply). A client-side `kubectl apply` stores the whole ConfigMap in `last-applied-configuration` and fails once `main.py` is this large.

Until `GUEST_UPSTREAM` is set, `/app/guest` serves the bundled teammate app (`video_ask_app.py`), mounted with Werkzeug so its `/` and `/api/ask` stay under `/guest`. The Flask `guest()` route that shows the “this button is free” page only runs when that file is missing. After `GUEST_UPSTREAM` is set, `/guest` is forwarded to that Service instead. `deploy.sh` does not set, unset, or restore `GUEST_UPSTREAM`.

`apps.json` fields:

| Field | Meaning |
|-------|---------|
| `id` | `upscaler`, `guest`, and `hazard` are built in. Anything else is appended. |
| `title` | Ignored for the three built-in ids. `_catalog()` sets Video Recutter, LDN Video Ask, Hazard perception. |
| `blurb` | Card text. |
| `live_blurb` | Guest only, used when `GUEST_UPSTREAM` is set. |
| `href` | Forced to `upscaler`, `guest`, or `hazard` for those ids. |
| `ready` | Hazard is always ready. Guest is ready when an upstream is set, otherwise the flag from the file. |

## Files

| File | Role |
|------|------|
| `main.py` | Flask app: launcher, recutter page (`INDEX_HTML`), VSS client, ffmpeg cut, guest and hazard proxies |
| `apps.json` | Launcher card blurbs |
| `requirements.txt` | `flask`, `requests`, `edge-tts` |
| `video_ask_app.py`, `vss_client.py`, `index.html`, `app.js`, `style.css` | Bundled LDN Video Ask, served at `/guest` when `GUEST_UPSTREAM` is empty |
| `deploy.sh` | Refreshes `hackathon-app` only |
| `SEED.md` | This note |
| `TEAM_HANDOVER.md` | Short product note for Discord |
| `TEAM_HANDOVER_ENV.md` | How to get onto the VM |
| `GROK_HANDOVER.md` | Shot-count fix and push notes for the next agent |

Kubernetes objects (not in git): Deployment/Service/Ingress `hackathon-app`, ConfigMap `hackathon-app-code`, Secret `hackathon-app-vss-creds`. Image is `python:3.12-slim`. Code is mounted at `/code`. The cut calls `ffmpeg` and `ffprobe`. Captions use DejaVu Sans. Spoken lines use `edge-tts`, then `espeak-ng` if the neural voice fails. This note does not claim the Deployment installs `espeak-ng`. If that binary is missing, the cut still renders and `voice_note` says the voiceover was skipped.

`deploy.sh` may also put `gpu-bearer`, `chunks-bucket`, `wandb-key`, `wandb-team`, and `wandb-project` into the ConfigMap when those values are available on the VM. It does not print them.

## Environment

| Variable | Where it comes from | Used for |
|----------|---------------------|----------|
| `PORT` | Deployment, `8080` | Bind address |
| `VSS_URL` / `VSS_INTERNAL_URL` | Secret | Retrieval API. Public lab hosts are rewritten to `http://video-backend-service:8000` |
| `VSS_USERNAME` / `VSS_PASSWORD` | Secret | `POST /api/v1/auth/login` |
| `WANDB_API_KEY` / `WANDB_TEAM` / `WANDB_PROJECT` | Env, else `/code/wandb-*`, else the team config | Optional brief and shot-list LLM |
| `WANDB_MODEL` | Optional | Default `meta-llama/Llama-3.1-8B-Instruct` |
| `COSMOS3_REASON_URL` / `COSMOS3_REASON_MODEL` | Env | Cosmos Reason chat. If the URL host does not resolve, the code uses a hardcoded fallback in `main.py` |
| `GPU_BEARER_TOKEN` or `NVIDIA_API_KEY` | Env, else `/code/gpu-bearer`, else the team config | Cosmos Reason bearer. Never log it |
| `S3_CHUNKS_BUCKET` | Env, else `/code/chunks-bucket`, else the team config | Build an `s3://` URI after upload when the response has only an object key |
| `GUEST_UPSTREAM` | Deployment env | Card 2. Left alone by `deploy.sh` |
| `HAZARD_UPSTREAM` | Deployment env | Card 3. Default `http://hazard-perception:80` |

Do not print the secret, do not commit it, do not `kubectl get secret -o yaml` into chat.

## HTTP surface (what the container sees)

Ingress rewrite means the browser's `/app/health` is the container's `/health`.

| Container path | Browser path | Purpose |
|----------------|--------------|---------|
| `GET /` | `/app/` | Launcher |
| `GET /upscaler` | `/app/upscaler` | Recutter page |
| `GET /guest` | `/app/guest` | Video Ask, or proxy |
| `/hazard/…` | `/app/hazard/…` | Hazard proxy. Failure text is `Hazard perception is not reachable` (502) |
| `GET /health` | `/app/health` | `{"status":"ok","app":"idea-upscaler","wandb":true\|false}` |
| `POST /api/expand` | `/app/api/expand` | Fill logline, beats, and transcript. No search, no cut |
| `POST /api/upscale` | `/app/api/upscale` | Search, and a rendered cut when matches exist |
| `POST /api/upload` | `/app/api/upload` | Multipart forward to `POST /api/v1/videos/upload` |
| `GET /api/upload-seed?tag=` | `/app/api/upload-seed` | Preview an indexed upload tag |
| `GET /api/stream?source=s3://…` | `/app/api/stream` | Proxied VSS segment |
| `GET /api/cuts/<12 hex>` | `/app/api/cuts/…` | Rendered mp4 |
| `POST /api/feedback` | `/app/api/feedback` | Append one JSON line to `/tmp/idea-feedback.jsonl` |

The page hardcodes `/app/api/...` and `<base href="/app/">`. A relative `api/upscale` from `/app/upscaler` would miss this process and hit the VSS UI HTML. That was the `Unexpected token '<'` bug.

`POST /api/upscale` body the page sends:

```json
{
  "metaprompt": "direction line, optional",
  "logline": "required unless a direction or a seeded upload supplies one",
  "beats": "optional, one beat per line",
  "transcript": "optional, one spoken line per line",
  "voice": "en-US-JennyNeural | en-US-GuyNeural | en-GB-SoniaNeural | en-GB-RyanNeural | off",
  "bed": 0.16,
  "duration_sec": null,
  "shot_count": 4,
  "seed_from_upload": false,
  "upload_tag": "my-upload",
  "scenarios": ["optional", "up to three"],
  "health_safety": false,
  "cc_broll": false,
  "use_form_brief": false
}
```

`duration_sec: null` is Automatic. A number must be 4–90. `shot_count` is clamped to 2–12. Response fields that matter: `shots[]`, `directors_cut[]`, `source_upload`, `rendered_cut` (`url`, `target_duration_sec`, `pieces`, `timeline`, `vo`, `voice_engine`), `render_error`, `shot_count`, `duration_sec`, `splitter`, `cosmos_reason`. Failures inside `upscale()` are still HTTP 200 with `render_error`, so a 5xx HTML page from the ingress does not replace the JSON.

## Pipeline inside `main.py`

**Login.** `_login` caches the JWT for about 25 minutes. `_vss` sends `Authorization: Bearer`. On 401 it logs in once more. Playback uses `?token=` on the VSS stream URL. The browser talks to `/api/stream`.

**Fill the brief.** `_expand_direction` asks Cosmos Reason (`/v1/chat/completions`, then `POST /api/v1/agent/ask` if that chat returns nothing), then W&B `https://api.inference.wandb.ai/v1/chat/completions`. `/api/expand` sets `allow_heuristic` false, so a failed model is an error. Recut, when the direction is set and the form was not just filled, calls the same function and may fall back to `_heuristic_direction` (the typed line split into the shot count, with a fixed night-NYC bike list when the line is a night ride and the count is 5 or less). If the direction matches the last successful fill and the three text fields are non-empty, Recut sends `use_form_brief` and skips another model call. That path also turns the level-crossing flag off.

**Shot list when there is no direction.** `_wandb_shots`, then `_heuristic_shots` if W&B returns nothing. `_pad_shots` fits the list to `shot_count` by trimming or cycling. After search, if this path did not already build a plan, `_apply_cosmos_reason` may reorder matches and rewrite the spoken lines. If Cosmos Reason is unavailable the cut continues with `cosmos_reason: "fallback"`. A direction plan skips that reorder.

**Search.** `_search_shot` posts `top_k` 5 (widened to 24, then 100, when sources are excluded), `llm_top_n` 1, `min_similarity` 0.2, `include_public` true, `time_filter` `all`. `llm_top_n` must be at least 1. Scenario lines are prepended to the query and also sent as `system_prompt`. Hits come from `results[]`, then `chunk_results[]`. Each hit's `playback_url` is `/app/api/stream?source=...`. The same parent video is not used twice in one recut.

**Stated cut.** `_parse_cut_instructions` reads a count only from text like `6 shots` or `six shots`, and a duration from `one minute`, `N minutes`, or a seconds figure that is not the per-shot length. Scenario lines win, then the Direction line fills only the gaps. Nothing here defaults to six. If the text states a count and no length, and the control is Automatic, `duration_sec` stays unset.

**Upload seed.** Tags are filters, not captions. Querying the tag string itself returns nothing. `_load_upload_seed` searches with visual queries (`hands holding a small toy figure`, then broader ones) and keeps rows for that tag. The indexed object for the original demo upload is `20261017_100245_8d0e1f47.mp4`, not the name the upload form showed.

**Cut.** `_render_same_length_cut`:

1. Target length is `duration_sec` when it is at least 4 (capped at 90). Otherwise it is the sum of the seeded upload clips. If that sum is under 0.5s, the target is 4 seconds times the number of slices.
2. `_select_cut_sources` keeps that many distinct clips. A short list is cycled so the slices stay equal. They are not stretched to fill the timeline.
3. Scale each to 1280x720, 24 fps. Keep the clip audio. Loop a short clip to fill its slice.
4. Concat, then trim to the target.
5. Spoken lines: `edge-tts` (Microsoft neural voice, default `en-US-JennyNeural`), mixed over the footage at the bed level (page default Quiet `0.16`; the API default if the field is omitted is `0.18`). `espeak-ng` is the fallback. The same lines are burned as DejaVu Sans subtitles. `voice: "off"` skips both. A level-crossing request that does not send a voice uses `en-GB-SoniaNeural`.

Files land in `/tmp/idea-cuts/<id>/cut.mp4`. Feedback lines land in `/tmp/idea-feedback.jsonl`. The pod disk is ephemeral. A restart drops rendered cuts and feedback.

**Level crossing.** Five shots from `_level_crossing_shots`. `max_pieces` is 5, so a scenario line that says “6 shots” does not change the number of slices. Cosmos Reason still runs on this path when it is reachable, and may shorten the match list; `_select_cut_sources` cycles that list back to five. Budget is 80 seconds. No archive hit and CC B-roll off returns `No level crossing in the archive. Tick CC B-roll if you want Commons footage.` Commons failures say so and do not substitute a crosswalk.

**CC B-roll on a normal recut.** At most one Commons clip, and only for a missing or weak match (similarity under 0.35). License text has to look like Creative Commons or public domain. The timeline row keeps title, author, and license.

**Errors.** The JSON error handler exists so `fetch` never receives Flask's HTML 500 page. A `LevelCrossingFootageError` is HTTP 200 with `render_error`.

## Deploy again

From the VM, in this repo:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
tools/idea-upscaler/deploy.sh
```

That server-side applies ConfigMap `hackathon-app-code`, restarts `deploy/hackathon-app`, and waits until `/app/upscaler` returns 200. It does not roll `hazard-perception`. ConfigMap updates do not show up in a running pod until the pod is recreated.

Open the app from the workshop lab page **App** button. Hard-refresh after a restart. Do not send people to the Ingress host as the product URL.

The Deployment's start command must keep installing ffmpeg. A fresh `python:3.12-slim` pod without that line will boot and then fail the cut with “ffmpeg not found”.

## Checks

```bash
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/health
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/ | head
```

The second command should be the launcher (`Team 41`), not the recutter form. The form is `/app/upscaler`.

A short recut (search plus ffmpeg; slow):

```bash
curl -sS -m 180 -X POST http://video-lab-team-41.cosmos.vastdata.com/app/api/upscale \
  -H 'Content-Type: application/json' \
  -d '{"logline":"Dusk ride through the city.","beats":"Wide city street\nCyclist in the bike lane\nPedestrians at a crosswalk\nCyclist paused","transcript":"Another night ride.\nThe city knew.","shot_count":4,"voice":"en-US-JennyNeural","bed":0.16}'
```

Expect `rendered_cut.target_duration_sec` (with no stated length and no upload, that is 4 seconds times the slice count), `vo: true` when edge-tts or espeak succeeded, and `GET` of `rendered_cut.url` returning `video/mp4`.

## Failures already hit

| Symptom | Cause |
|---------|--------|
| HTML where JSON was expected | Frontend called a path outside `/app`, or Flask returned an HTML traceback |
| Search always empty | `llm_top_n: 0`, or the query was the tag string |
| Login DNS failure in the pod | `VSS_URL` was the public Ingress host |
| `502` right after restart | Request arrived while the new pod was still in `apt-get` |
| Cut missing | ffmpeg not found, or no match `source` |
| ConfigMap apply “Too long” | Client-side apply. Use `deploy.sh` |
| Voice missing, cut still plays | `edge-tts` failed and `espeak-ng` is not installed. Read `voice_note` |
| Level crossing used a street | Should not. The filter rejects a crosswalk. If the archive has no railway clip, tick CC B-roll or accept the archive note |
| Second app 404 on the public host | Their Service is only reachable as `GUEST_UPSTREAM`, via `/app/guest` |
| Hazard card 502 | `hazard-perception` is down. `deploy.sh` does not restart it |

## Archive this build was written against

Team 41, namespace `team-41`. Mostly streets, traffic, a warehouse, and an indoor crowd camera. The rough demo upload is the tagged upload: a hand moving a small white toy. Search stand-ins come from that corpus (street and bike footage), not from a generated scene. The index has no railway footage, which is why the level-crossing brief looks outside the archive only when CC B-roll is on.

## Using this as a seed

Keep the launcher, the `/app` prefix, the in-cluster VSS client, and `deploy.sh`. Replace the recut path if the product is different. Leave the guest and hazard cards so the next person does not have to fight you for the App button.
