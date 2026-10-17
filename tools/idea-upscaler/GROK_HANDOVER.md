# Handover for the next Grok

## Repo and push rules

- Live checkout: `/home/onkarbhattacharya/hackerthon--real-time-video--LDN`
- `/home/samialsindi/vast-builders-challenge` is not on this machine. Do not assume the two paths are the same.
- Branch: `idea-upscaler`
- Team remote URL: `https://github.com/ahmedm87/hackerthon--real-time-video--LDN.git`
- Push only that URL, only branch `idea-upscaler`, HTTPS only. Never push `origin`. Never force-push. Never use SSH or port 22.
- This clone has no saved remote named `teamrepo` (`origin` and `fork` are a different GitHub user; `upstream` is the team URL). Do not edit git config to add a remote. Push with a one-shot override:

```bash
git -c remote.teamrepo.url=https://github.com/ahmedm87/hackerthon--real-time-video--LDN.git push teamrepo HEAD:idea-upscaler
```

- Git identity is empty. Do not set `user.name` or `user.email`. On the commit command only:

```bash
GIT_AUTHOR_NAME=samialsindi
GIT_AUTHOR_EMAIL=samialsindi@bc-pool-233.cosmo.vastdata.com
GIT_COMMITTER_NAME=samialsindi
GIT_COMMITTER_EMAIL=samialsindi@bc-pool-233.cosmo.vastdata.com
```

- Do not commit `/config`, kubeconfigs, credentials, or real values from config. Do not commit `data/last_run.json`.

## Current product

Card 1 is Video Recutter. The page, routes, and limits are in `SEED.md`. This note is the shot-count bug and the push rules. Do not treat the bug section as the whole app.

Fill the brief, Cosmos Reason, spoken voiceover, upload, CC B-roll, and the five-beat level-crossing path are implemented. The old buttons **Load demo brief** and **Upscale idea** are gone. The demo chip is **Streets · NYC bike**. The action button is **Recut**.

## Config-window prompt

The prompt is the three scenario lines on Video Recutter, labeled **Scenarios for the VAST reasoner (1–3)**.

- File: `tools/idea-upscaler/main.py` (the page is the `INDEX_HTML` string)
- Fields: `#scenario1`, `#scenario2`, `#scenario3`, filled from `#scenarioPreset`
- Browser: `scenarioLines()` sends them as `scenarios` on `POST /app/api/upscale`
- Server: `upscale()` keeps them with `_clean_scenarios`

Before this fix those lines were only search text (`_query_for_scenarios` / `_search_shot`) and, on upload, one VSS `custom_prompt` (`_scenario_custom_prompt`). They did not set the shot count or the length.

## The bug

The Shots control defaulted to 4 and was clamped with `max(2, min(5, shot_count))`. A filled brief then did `count = min(shot_count, len(form_beats), len(form_lines))`, so four beats won. `_render_same_length_cut` kept at most that many clips and set `slice_len = target / len(picks)`.

A one-minute cut whose scenario prompt asked for 6 shots therefore became four clips stretched across 60 seconds:

- before: 0–15, 15–30, 30–45, 45–60 (15s each)

## The fix

`_parse_cut_instructions` reads a shot count and a duration only when the text states them. `_stated_cut_from_prompts` checks the scenario lines first. The Direction line fills a field only when the scenario text did not state it. Six is not a default. If neither prompt states a count, the Shots control stays as set (default 4).

`_equal_shot_spans` and `_select_cut_sources` build exactly that many slices of `duration / count`. Fewer source clips are repeated into the remaining slices. They are not widened to fill the minute.

After, for the scenario text `one minute cut of 6 shots` (Shots control still 4):

- 0–10, 10–20, 20–30, 30–40, 40–50, 50–60

The same field with `one minute, 3 shots` is 3 × 20s. `8 shots of 5 seconds` is 8 × 5s. `six shots at 10 seconds each` is 6 × 10s. A scenario with no count leaves the control at 4. A checked ffmpeg render of four source files with that 6-shot minute plan produced a 60.0s file and six 10s timeline rows.

## How to reproduce

1. Open Video Recutter (`/app/upscaler`).
2. In scenario 1 type `one minute cut of 6 shots`. Leave Shots at 4.
3. Recut. The timeline is six segments of 10 seconds, and the response `shot_count` is 6 with `duration_sec` 60.
4. Change that same box to `one minute, 3 shots` and recut. The plan becomes three segments of 20 seconds.

## Deploy

```bash
KUBECONFIG=/config/team-41-k8s.yaml tools/idea-upscaler/deploy.sh
```

- Namespace: `team-41`
- Kubeconfig: `/config/team-41-k8s.yaml`
- Live path: `/app` on the team ingress (`video-lab-team-41.cosmos.vastdata.com`). People open it from the workshop App button.
- No docker build. `deploy.sh` updates ConfigMap `hackathon-app-code` and restarts `deploy/hackathon-app`.
- Client-side `kubectl apply` stores the whole ConfigMap in `last-applied-configuration` and fails once `main.py` is large (`metadata.annotations: Too long: may not be more than 262144 bytes`). The script now uses `kubectl apply --server-side --force-conflicts`.
- The last recorded run of that script rolled out and `http://video-lab-team-41.cosmos.vastdata.com/app/upscaler` returned 200. That HTTP check was not repeated for this doc pass. In the current page source the scenario boxes exist, shot `max="12"` `value="4"`, and the hint says a stated shot count or length overrides the Shots and Seconds controls.

## Card 2

LDN Video Ask is card 2. Do not lock it. Do not change `GUEST_UPSTREAM`. `deploy.sh` does not set, unset, or restore that variable.

Last recorded on the deployment after the recutter rollout, and not re-checked for this doc pass: `GUEST_UPSTREAM` was `http://near-miss-app:80`. The older note that it must be `http://ldn-video-ask:80` was not what the cluster had then. The code does not set the variable. Leave the live value alone.

## Card 3

`/app/hazard` is routed in code. `hazard()` in `main.py` proxies `HAZARD_UPSTREAM` or `http://hazard-perception:80`. After the recutter rollout, `GET /app/hazard` returned 200 and `HAZARD_UPSTREAM` was still `http://hazard-perception:80`. That cluster check was not repeated for this doc pass. The hazard page itself is `tools/hazard-perception/main.py`. `deploy.sh` does not roll that Deployment.

## Git

- Shot-fix commit: `f9b19377e9e46c1038bd1d326f7ede28a3b6520d`
- Handover commit: `0885ba282a5ba658969dee19427efe750e518b67`
- Deploy-script commit: `637b21b7c71b18a4713bfa5538834d679c5774be` (server-side ConfigMap apply; this is what was rolled out)
- Push to `teamrepo` `idea-upscaler` failed. GitHub authenticated as OnkarBhattacharya, who cannot write to `ahmedm87/hackerthon--real-time-video--LDN`. The commits are local only until an account with write access pushes. Do not push `origin` to work around that.

```text
remote: Permission to ahmedm87/hackerthon--real-time-video--LDN.git denied to OnkarBhattacharya.
fatal: unable to access 'https://github.com/ahmedm87/hackerthon--real-time-video--LDN.git/': The requested URL returned error: 403
```

## Still true limits

- A level-crossing recut (the safety chip, no Direction typed) still renders five slices from its own five-beat storyboard. A shot count in the scenario text does not change `max_pieces`. Cosmos Reason may reorder the railway matches; a shorter list is cycled back to five. Typing a Direction and recutting leaves the level-crossing path.
- If the scenario prompt names a shot count but no length, and Cut length is Automatic, `duration_sec` stays unset. The cut then uses the summed duration of seeded upload clips, or 4 seconds times the slice count when there is no source duration.
- A scenario line is still also a search hint, so a line that is only "6 shots, one minute" does not describe a picture. Put the picture in the other scenario boxes or in Direction.
- `POST /api/upscale` still returns a `pitch` string from the old boards-and-voice line. The page does not render it.
