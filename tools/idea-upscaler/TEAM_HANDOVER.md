# Team 41 — Video Recutter handover (paste-ready for Discord)

**Status:** Live on the workshop **App** button, as card 1 of three.  
**Page line:** *Type one line. Fill the brief writes the film. Recut makes the video.*

The code directory is still `tools/idea-upscaler/`. The health JSON still says `app: idea-upscaler`. The product name on the card and the page is **Video Recutter**.

**Also see:** [`TEAM_HANDOVER_ENV.md`](./TEAM_HANDOVER_ENV.md) — workshop login, SSH, Cursor, cloudflared, buttons, and all the setup fuckery to get here. Behavior detail is [`SEED.md`](./SEED.md). Hazard perception is [`../hazard-perception/SOLUTION_ARCHITECTURE.md`](../hazard-perception/SOLUTION_ARCHITECTURE.md).

---

## What we built

The **App** button opens a launcher. Three cards:

1. **Video Recutter** (`/app/upscaler`) — a direction or a brief, then a cut from the already-indexed archive. Spoken voiceover plus burned captions. Not generative video.
2. **LDN Video Ask** (`/app/guest`) — the bundled ask app, or whatever `GUEST_UPSTREAM` points at.
3. **Hazard perception** (`/app/hazard/`) — a separate pod. Mark a hazard, then pick Slow down / Brake / Wait / Steer away / Continue. One YOLO box. Local score, 0 to 5.

Recutter, in the order a person actually uses it:

1. Type a **Direction**, or click a brief chip (**Streets · NYC bike** is the short one).
2. **Fill the brief** writes the logline, the shots, and the spoken lines. Cosmos Reason, then W&B. This step does not cut.
3. **Recut** searches Team 41’s VSS archive and ffmpeg-cuts equal slices. Default 4 shots, range 2–12. Length is Automatic unless you pick Custom seconds or the scenario text states a count or a length.
4. UI: shot list and matches on the left, **Director's cut** on the right (the rendered mp4 and a timeline).

**Not generative video.** No diffusion / no Sora. Retrieval, then an ffmpeg assembly. CC B-roll and the level-crossing brief can download one or more Wikimedia Commons clips. That is off unless the checkbox is ticked (level crossing also needs the checkbox before it leaves the archive).

---

## How to open it (everyone)

1. Go to **https://workshop.thecosmoslabs.com**
2. Open **Builders Challenge**
3. Click **App** (not Video Search & Summary — that’s the stock VSS UI)
4. Hard-refresh once (`Cmd/Ctrl+Shift+R`) if you still see an old error
5. Open **Video Recutter**. Hazard perception is the third card.

Do **not** use localhost. Do **not** redeploy the VSS stack.

**Demo path (30s):**  
**Streets · NYC bike** → **Recut** → play Director's cut.

There is no **Load demo brief** button and no **Upscale idea** button. Those were the previous page.

---

## How it works (architecture)

```
Browser (workshop App)
   ↓  /app  (Ingress rewrite → pod /)
hackathon-app (python:3.12-slim + ConfigMap code)
   ↓  login + search + stream proxy + ffmpeg cut
   ↓  /hazard  →  hazard-perception:80
video-backend-service:8000  (in-cluster VSS API)
   ↓
VastDB index (Team 41 corpus)
```

| Piece | Detail |
|--------|--------|
| Code | `tools/idea-upscaler/main.py` (page is the `INDEX_HTML` string) |
| Hazard | `tools/hazard-perception/main.py`, its own Deployment |
| K8s | Deployment/Service/Ingress `hackathon-app` in namespace `team-41` |
| Ingress | Host `video-lab-team-41.cosmos.vastdata.com`, path **`/app`** |
| Creds | Secret `hackathon-app-vss-creds` — never commit, never print |
| Brief | Cosmos Reason, then W&B, then a local split only inside Recut |
| Search | `POST /api/v1/search` per shot |
| Cut | ffmpeg in the pod. Files in `/tmp/idea-cuts`. A restart drops them |
| Playback | `GET /app/api/stream?source=s3://…` → VSS `/videos/stream` |

**Important bug we fixed:** pods cannot DNS the external Ingress hostname. `VSS_URL` must be **`http://video-backend-service:8000`** (in-cluster). The app also auto-rewrites `*.cosmos.vastdata.com` → that Service.

**Another bug we fixed:** search API requires `llm_top_n >= 1` (sending `0` → 422 → empty results → UI looked broken).

**JSON error:** earlier “Unexpected token `<!doctype`…” meant the UI got an HTML error page instead of JSON (bad `/app` relative paths and/or Flask 500 HTML). Fixed with `/app/api/...` absolute paths + JSON error handler.

**Shot count:** a line such as `one minute cut of 6 shots` in the scenario boxes overrides the Shots control (default 4, max 12). The timeline is equal slices. Fewer clips are repeated, not stretched. Detail in `GROK_HANDOVER.md`.

---

## What’s in the archive (so demos don’t flop)

Genre-ish catalogue for Team 41:

| Pack | Vibe | Examples |
|------|------|----------|
| Highway traffic | Overhead multi-cam | Nashville `i24_cam-1` |
| Live driving | Dashcam / road | Toronto `pie_cam-3` |
| City streets | Bikes, pedestrians, intersections | NYC GoPro + street cams; SF streets |
| Neighborhood | Residential street life | `neighborhood_cam-1` |
| Warehouse | Aisles / ops | `warehouse3` |
| Indoor / crowds | Facility people-flow | `smartspace_cam-1` |

**Best recutter demos** = urban dusk rides, traffic, street life, warehouse ops.  
**Bad demos** = fantasy / studio drama that isn’t in the corpus.  
**Level crossing** is not in the archive. The safety chip says so unless CC B-roll is ticked.

---

## How teammates can use / iterate

### Just demo
Workshop → App → Video Recutter → Streets · NYC bike → Recut.

### Change the product
1. Edit `tools/idea-upscaler/main.py` on the VM (Cursor SSH). Hazard edits are `tools/hazard-perception/main.py` and a separate rollout. `deploy.sh` does not restart that pod.
2. Redeploy the launcher and recutter only:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
tools/idea-upscaler/deploy.sh
```

3. Hard-refresh **App**.

### Check health

```bash
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/health
# expect: {"status":"ok","app":"idea-upscaler","wandb":...}
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/hazard/health
# expect: {"status":"ok","app":"hazard-perception"}
```

### Logs if something breaks

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 logs -l app=hackathon-app --tail=100
```

### Skills used (don’t invent raw REST without reading these)
- `.cursor/skills/retrieval/login`
- `.cursor/skills/retrieval/search`
- `.cursor/skills/retrieval/videos`
- `.cursor/skills/deployment/deploy-app-no-registry`

---

## Multiple apps?

**One App button / one `/app` Ingress path** per team.  
Three products share that path: the launcher, Video Recutter, and proxies for guest and hazard. Stock VSS UI stays at `/` via **Video Search & Summary**. Replacing the `hackathon-app` ConfigMap does not remove the hazard Deployment.

---

## Acceptance checklist

- [x] Lab page → **App** loads the Team 41 launcher
- [x] Video Recutter recut returns JSON (not HTML) and a playable cut when the archive has matches
- [x] Hazard perception is a third card, scored locally
- [x] README section with the open path and a 30s demo
- [x] No localhost deliverable; VSS stack not redeployed

---

## If you’re stuck

1. Hard-refresh App
2. Confirm the health curls above
3. Check pod logs
4. Empty search? Broaden the brief toward **streets / traffic / warehouse** — don’t assume a code bug first
5. Use `ask-cosmos` skill if the stack itself is unhealthy

Ship line for Discord: **Team 41 App is a launcher. Card 1 is Video Recutter (direction or brief → archive cut). Card 3 is Hazard perception. Open workshop → App.**
