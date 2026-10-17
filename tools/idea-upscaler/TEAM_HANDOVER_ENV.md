# Team 41 — Environment / login / “how the fuck did we get here” handover

Paste-ready for Discord. Companion to `TEAM_HANDOVER.md` (the app). This one is **how to get onto the machine and not die on setup**.

**Team:** 41 · **VM label:** `bc-pool-233` (lab page) · **SSH host name you actually use:** `bc-samialsindi`  
**Username on VM:** `samialsindi` · **K8s namespace / VSS user:** `team-41`

---

## TL;DR path that works

1. Browser → **https://workshop.thecosmoslabs.com** → log in with **work email**
2. Open **Builders Challenge** lab page
3. Install SSH public key (once)
4. Configure laptop (`cloudflared` + `~/.ssh/config`) — lab page Option 1 or 2
5. **Connect once in a normal terminal** (`ssh bc-samialsindi`) so Cloudflare login sticks
6. Cursor → **Connect via SSH** → pick `bc-samialsindi` → open `vast-builders-challenge`
7. Agent model: **Auto** (saves tokens)
8. Explore: lab **Video Search & Summary** (stock UI) · our app: lab **App**

**Do not** skip step 5. Cursor cannot finish Cloudflare auth for you.

---

## 1. Workshop login (everyone)

| Step | What |
|------|------|
| URL | https://workshop.thecosmoslabs.com |
| Auth | Work email → Continue (returning users auto-sign-in) |
| Lab | Click **Builders Challenge** |
| What you should see | Lab environment **Running · Team 41**, buttons: Build Day Guide, Dashboard, **Video Search & Summary**, **App**, Open desktop |

**Buttons that matter**

| Button | What it is | When to use |
|--------|------------|-------------|
| **Video Search & Summary** | Stock VSS UI (Search / Explore / Dashboard) | Browse index, try suggested searches, sanity-check clips |
| **App** | Team 41 launcher at Ingress `/app` (Video Recutter, LDN Video Ask, Hazard perception) | Demo / iterate the product |
| **Open desktop** | Full VM desktop in browser | Only if you’re *not* on Cursor Remote-SSH. Optional fallback. |
| Dashboard | Workshop status screen | Ops / organizers view |

**Popup fuckery:** Video Search opens a named popup (`builders-vss`). Allow pop-ups for `workshop.thecosmoslabs.com`. If you get a blank tab: close blank popups → hard-refresh lab page → click again. Don’t let an agent drive that button from Cursor’s embedded browser — it breaks the SSO popup flow.

---

## 2. SSH key (step 1 on the lab page)

Lab page says: username is **`samialsindi`**. Install a public key so the VM trusts your laptop.

**Recommended (new key just for this lab)**

```bash
# Mac/Linux
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_cursor -C "builders-lab"
cat ~/.ssh/id_ed25519_cursor.pub
```

Paste the `ssh-ed25519 AAAA…` line into the lab page box → **Install public key**.  
Private key stays on your laptop. Never paste the private key anywhere.

If you already have a key: paste *that* `.pub` instead.

Windows: use PowerShell / WSL as on the lab page; same idea — generate, paste **public** key.

---

## 3. Laptop access (cloudflared + SSH config)

The VM is behind Cloudflare. You need `cloudflared` and an SSH host alias.

### Order that actually works

1. Key installed on lab page (above)
2. Lab page **Option 1 · Prompt Cursor Faster** *or* **Option 2 · Do it yourself** (pick your OS: Mac / Linux / Windows)
3. After config exists, in a **real terminal** (not Cursor yet):

```bash
ssh bc-samialsindi
```

4. Browser Cloudflare login opens → finish it once  
5. You’re on the VM shell → you can `exit`  
6. **Then** open Cursor → Connect via SSH → `bc-samialsindi`

### Gotchas that waste hours

| Trap | Reality |
|------|---------|
| `ssh bc-pool-233.thecosmoslabs.com` | **Wrong.** Use the alias **`bc-samialsindi`**. Full name skips the SSH config and fails. |
| Opening Cursor before terminal SSH | Cloudflare cookie/login not saved → Cursor hangs / auth loops |
| No IdentityFile | Lab config tries `~/.ssh/id_ed25519_cursor` then default keys. If you made a custom key path, put it in `~/.ssh/config` |
| macOS blocks cloudflared | System Settings → Privacy & Security → Open Anyway |
| “Open desktop” instead of Cursor | Fine for browsing; **not** where the coding agent + skills live. Code work = Cursor Remote-SSH |

---

## 4. Cursor on the VM

1. New Cursor window → **Connect via SSH** → `bc-samialsindi`
2. Open folder: **`vast-builders-challenge`** (repo is pre-cloned on the VM)
3. Agent panel model → **Auto**
4. Cursor credits: check https://cursor.com/dashboard/usage (should show Free for registrants). Need more → organizers’ form in README

**What lives on the VM (not your laptop):**

- Repo + `.cursor/skills/`
- `/config/team-41.config` (credentials / endpoints — **never commit, never paste secrets in Discord**)
- `/config/team-41-k8s.yaml` (kubeconfig)
- Env vars already sourced for skills (`INGRESS_URL`, `USERNAME`, `PASSWORD`, `WANDB_*`, etc.)

**Safe check that env exists (names only):**

```bash
env | cut -d= -f1 | sort | grep -Ei 'INGRESS|USERNAME|WANDB|PIPELINE'
```

Never run bare `env` / `printenv` in Discord screenshots — secrets leak.

---

## 5. Mental model of the stack

```
Your laptop (Cursor Remote-SSH)
        ↓
Lab VM (this repo + /config + kubectl)
        ↓
K8s namespace team-41
  ├── VSS UI + API  (Ingress host video-lab-team-41.cosmos.vastdata.com)
  │     Search / Explore / Dashboard  ← “Video Search & Summary”
  ├── hackathon-app  path /app        ← workshop “App” button (launcher + Video Recutter)
  └── hazard-perception               ← no Ingress of its own; the launcher proxies `/app/hazard`
```

- **Skills** in `.cursor/skills/` wrap the VSS APIs (login, search, videos, deploy-app-no-registry, …). Prefer skills over inventing curl.
- **Do not redeploy the VSS stack** for the hackathon app. Only deploy *our* mini-app to `/app`.
- **WANDB_*** = serverless LLM for brief filling and shot splitting when Cosmos Reason does not answer. VSS Cosmos models are already in the ingest/search pipeline. Video Recutter also calls Cosmos Reason directly for the brief. Hazard perception does not.

---

## 6. Day-one product loop (what we did before building)

1. Lab → **Video Search & Summary** → try 2 suggested searches → play a clip  
2. In Cursor: *what’s in the index?* (dashboard / explore)  
3. Search for an idea → ask over a video → find a gap → re-ingest if needed  
4. Build the App (Video Recutter in `tools/idea-upscaler/`, Hazard perception in `tools/hazard-perception/`) → `tools/idea-upscaler/deploy.sh` for the launcher → workshop **App**

Guided version in Cursor: `/build-day-quickstart`

---

## 7. Common “fuckery” we’ve already hit

| Symptom | Cause | Fix |
|---------|--------|-----|
| Blank Video Search tab | Popup / SSO broken (esp. if agent clicked it) | Close blanks, allow popups, re-click from **your** browser |
| App JSON error `Unexpected token <!doctype` | UI got HTML (wrong path or Flask 500) | Hard-refresh App; see app handover. Paths must be `/app/api/...` |
| App upscale empty results | `llm_top_n: 0` invalid; or VSS URL = external host from inside pod | Fixed in app: `llm_top_n: 1`, VSS = `http://video-backend-service:8000` |
| kubectl Forbidden from agent sandbox | Needs real cluster network / kubeconfig | `export KUBECONFIG=/config/team-41-k8s.yaml` on the VM |
| Tempted to `python main.py` locally as deliverable | Against rules | Deliverable = K8s + Ingress `/app` only |
| Multiple apps | One `/app` Ingress | Launcher has three cards. Recutter and the proxies share `hackathon-app`. Hazard is its own Deployment |

---

## 8. If you’re blocked

1. Can you `ssh bc-samialsindi` from a terminal? If no → lab page steps 1–3 again.  
2. Is Cursor on the VM with `vast-builders-challenge` open?  
3. Health: ask Cursor to run login + dashboard skills, or `/ask-cosmos` (writes a note — **you** post it to Cosmos community).  
4. App down: `curl -sS http://video-lab-team-41.cosmos.vastdata.com/app/health` and `kubectl -n team-41 logs -l app=hackathon-app --tail=100`

---

## 9. Who does what on the team

| Role | Do this |
|------|---------|
| Anyone demoing | Workshop → **App** (and optionally Video Search for vibe) |
| Anyone coding | Cursor Remote-SSH as above; edit `tools/idea-upscaler/` or `tools/hazard-perception/` |
| Re-ingest heavy | 1–2 people only (README warning) — don’t all re-ingest hours of video |
| Discord paste | This file = env; `TEAM_HANDOVER.md` = product |

---

**Ship line:** *Get on workshop → key + cloudflared + `ssh bc-samialsindi` once → Cursor SSH → repo on VM. Stock UI = Video Search. Our product = App.*
