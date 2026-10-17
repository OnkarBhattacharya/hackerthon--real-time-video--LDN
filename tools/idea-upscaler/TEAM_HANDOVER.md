# Team 41 — Idea Upscaler handover (paste-ready for Discord)

**Status:** Live on the workshop **App** button.  
**Pitch:** *We start from boards and voice, not a prompt. The archive finds the cinematic version.*

**Also see:** [`TEAM_HANDOVER_ENV.md`](./TEAM_HANDOVER_ENV.md) — workshop login, SSH, Cursor, cloudflared, buttons, and all the setup fuckery to get here.

---

## What we built

**Idea Upscaler** — a small web app for directors/artists who think in **boards + notes + VO**, not text-to-video prompts.

1. You paste a **logline / scene intent** (required), optional **storyboard beats**, optional **VO/dialogue**.
2. The app turns that into a **shot list** (3–8 shots: type, mood, action, search query).
3. For each shot it **searches Team 41’s already-indexed VSS archive** for cinematic matches.
4. UI: shot list on the left → top clips + playback for the selected shot → **Director’s cut** (ordered shots + best match each).

**Not generative video.** No diffusion / no Sora. Pure **retrieval + assembly** on top of VSS.

---

## How to open it (everyone)

1. Go to **https://workshop.thecosmoslabs.com**
2. Open **Builders Challenge**
3. Click **App** (not Video Search & Summary — that’s the stock VSS UI)
4. Hard-refresh once (`Cmd/Ctrl+Shift+R`) if you still see an old error

Do **not** use localhost. Do **not** redeploy the VSS stack.

**Demo path (30s):**  
**Load demo brief** → **Upscale idea** → click a shot → play a clip → show Director’s cut.

---

## How it works (architecture)

```
Browser (workshop App)
   ↓  /app  (Ingress rewrite → pod /)
Idea Upscaler pod (python:3.12-slim + ConfigMap code)
   ↓  login + search + stream proxy
video-backend-service:8000  (in-cluster VSS API)
   ↓
VastDB index (Team 41 corpus)
```

| Piece | Detail |
|--------|--------|
| Code | `tools/idea-upscaler/main.py` (+ `requirements.txt`) |
| K8s | Deployment/Service/Ingress `hackathon-app` in namespace `team-41` |
| Ingress | Host `video-lab-team-41.cosmos.vastdata.com`, path **`/app`** |
| Creds | Secret `hackathon-app-vss-creds` (VSS + WANDB) — never commit |
| Shot split | W&B serverless inference if `WANDB_*` set; else line/beat heuristic |
| Search | `POST /api/v1/search` per shot (film-aware query) |
| Playback | App proxies `GET /app/api/stream?source=s3://…` → VSS `/videos/stream` |

**Important bug we fixed:** pods cannot DNS the external Ingress hostname. `VSS_URL` must be **`http://video-backend-service:8000`** (in-cluster). The app also auto-rewrites `*.cosmos.vastdata.com` → that Service.

**Another bug we fixed:** search API requires `llm_top_n >= 1` (sending `0` → 422 → empty results → UI looked broken).

**JSON error:** earlier “Unexpected token `<!doctype`…” meant the UI got an HTML error page instead of JSON (bad `/app` relative paths and/or Flask 500 HTML). Fixed with `/app/api/...` absolute paths + JSON error handler.

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

**Best Idea Upscaler demos** = urban dusk rides, traffic, street life, warehouse ops.  
**Bad demos** = fantasy / studio drama that isn’t in the corpus.

---

## How teammates can use / iterate

### Just demo
Workshop → App → Load demo brief → Upscale.

### Change the product
1. Edit `tools/idea-upscaler/main.py` on the VM (Cursor SSH).
2. Redeploy code only:

```bash
export KUBECONFIG=/config/team-41-k8s.yaml
kubectl -n team-41 create configmap hackathon-app-code \
  --from-file=tools/idea-upscaler \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n team-41 rollout restart deploy/hackathon-app
kubectl -n team-41 rollout status deploy/hackathon-app
```

3. Hard-refresh **App**.

### Check health

```bash
curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/health
# expect: {"status":"ok","app":"idea-upscaler",...}
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
We can only run **one** live hackathon app at a time (or one UI with tabs). Replacing ConfigMap + restart swaps the App. Stock VSS UI stays at `/` via **Video Search & Summary**.

---

## Acceptance checklist

- [x] Lab page → **App** loads Idea Upscaler  
- [x] Upscale returns JSON (not HTML)  
- [x] At least one shot gets playable archive matches  
- [x] README section with open path + 30s pitch  
- [x] No localhost deliverable; VSS stack not redeployed  

---

## If you’re stuck

1. Hard-refresh App  
2. Confirm health curl above  
3. Check pod logs  
4. Empty search? Broaden beats toward **streets / traffic / warehouse** — don’t assume a code bug first  
5. Use `ask-cosmos` skill if the stack itself is unhealthy  

Ship line for Discord: **Team 41 App is Idea Upscaler — boards+VO → shot list → archive cinema. Open workshop → App.**
