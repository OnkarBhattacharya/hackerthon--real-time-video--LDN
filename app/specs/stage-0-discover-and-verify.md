# Stage 0 — Discover and verify

**Time box:** 30 minutes
**Depends on:** team VM environment, `.cursor/skills/`, and the running VSS stack
**Blocks:** Stages 1–7. Do not write client code against guessed response shapes.
**Gate:** Stop after this stage. Show a short summary and wait until the operator types `go`.

## Outcome

`NOTES.md` records the live contracts this app will call: auth, search, segment and clip access, valid metadata values, the W&B chat model, and whether crops and audio are available.

`stages.md` refers to skills "named in section 2". That section is not in this file. Read these skills in full, because later stages call them:

- `retrieval/login`
- `retrieval/dashboard`
- `retrieval/list-metadata`
- `retrieval/search`
- `retrieval/videos`
- the `gpu/` skills that expose a frame or crop for a segment
- the W&B inference skill used to list models and send a chat request

Also note, but do not execute yet: `deployment/deploy-app-no-registry` and the submission skill. Stage 7 owns those.

## Steps

1. Run `git pull`. List `.cursor/skills/`. Read the skills above in full. In `NOTES.md`, summarise for each endpoint what it requires and what it returns, including path, method, auth header, and the field names later stages need.
2. Print environment variable **names only**. Confirm these exist:
   - team config values used by the retrieval login skill
   - `INGRESS_URL`
   - the W&B variables named by that skill
   - `COSMOS3_REASON_URL`, `YOLO_URL`, `COSMOS_EMBED1_URL`, `CANARY_1B_URL`
   List every missing name in `NOTES.md`. Do not print values.
3. Run the `retrieval/login` flow and the skill's "check that everything is working" check. Run `retrieval/dashboard`. Record indexed segment counts per camera.
4. Run `retrieval/list-metadata`. Record the exact valid values for `camera_id`, `location`, and `capture_type`.
5. Call `retrieval/search` once with the query `person close to a moving vehicle`. Save the raw response to `samples/search_response.json`. From that payload, record the fields for:
   - segment id
   - video id
   - start and end time
   - score
   - description text
   - detections
   - clip URL, or the exact call that produces one
6. Using `retrieval/videos`, write the exact calls for:
   - one segment's full details and detections
   - a playable clip URL
   - the previous and next segments of the same video
7. Call the W&B inference endpoint. List models, pick one capable instruction-following model, send one short chat request, and send one request that demands strict JSON. Record the base URL, the model name, whether JSON mode or a schema is supported, and what the JSON attempt actually returned.
8. Check whether detections include bounding boxes and whether a frame or crop can be fetched for a segment (`retrieval/videos` and `gpu/`). If no frame is available, set crop availability to `unavailable`.
9. Check whether any sampled clip has speech. If that cannot be determined inside this time box, mark audio features out of scope.

## Verification

`NOTES.md` contains a filled section for each step, including missing env names and the crop and audio decisions. `samples/search_response.json` is the unmodified search body.

These four must succeed before anything else is built:

- login and the working-check (step 3)
- one real search saved to disk (step 5)
- documented calls for segment details, clip URL, and neighbours (step 6)
- one W&B chat completion, plus the JSON-mode result (step 7)

## Done when

- The summary has been shown to the operator.
- Work is paused until they type `go`.
- `PROGRESS.md` has a Stage 0 entry: what worked, what is missing, and the crop and audio decisions.
