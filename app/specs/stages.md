5. Stages

Specs:

- [constraints.md](constraints.md) — architecture, quality, acceptance, fallbacks, scope cuts
- [stage-0-discover-and-verify.md](stage-0-discover-and-verify.md)
- [stage-1-data-layer.md](stage-1-data-layer.md)
- [stage-2-severity-ranking.md](stage-2-severity-ranking.md)
- [stage-3-evidence-assembly.md](stage-3-evidence-assembly.md)
- [stage-4-reports.md](stage-4-reports.md)
- [stage-5-api.md](stage-5-api.md)
- [stage-6-frontend.md](stage-6-frontend.md)
- [stage-7-deploy-and-submit.md](stage-7-deploy-and-submit.md)

Stage 0: Discover and verify (time-box 30 minutes)
1.  Run git pull. List .cursor/skills/ and read, in full, the skills named in section 2 that you will use. Summarise in NOTES.md what each endpoint needs and returns.
2.  Print environment variable NAMES only and confirm these exist: the team config values, INGRESS_URL, the WANDB variables, and the GPU endpoint variables (COSMOS3_REASON_URL, YOLO_URL, COSMOS_EMBED1_URL, CANARY_1B_URL). List any that are missing.
3.  Run the retrieval/login flow and run "check that everything is working". Run retrieval/dashboard and record counts of indexed segments per camera.
4.  Run retrieval/list-metadata and record the exact valid values for camera_id, location, capture_type.
5.  Call retrieval/search once with the query "person close to a moving vehicle" and save the raw response to samples/search_response.json. Study it: record the fields for segment id, video, start and end time, score, description text, detections, clip URL or how to get it.
6.  Using the retrieval/videos skill, work out how to (a) fetch one segment's full details and detections, (b) get a playable clip URL, (c) get the neighbouring previous and next segments of the same video. Record exactly how.
7.  Call the W&B inference endpoint: list available models, choose a capable instruction-following model, send one short chat request, and also test asking for strict JSON output. Record the base URL, model name and how JSON output behaved.
8.  Check whether detections include bounding boxes and whether a frame or crop can be obtained for a segment (see the videos and gpu/ skills). Record the result. If no frame is available, mark "crop" as unavailable.
9.  Check whether any sampled clip has speech (audio). If none can be determined quickly, mark audio features as out of scope.
Gate: Do not proceed until steps 3, 5, 6 and 7 work. Show me a short summary and wait for me to type "go".
Stage 1: Data layer (time-box 45 minutes)
Build backend/ with FastAPI. Create backend/vss.py, a thin client that wraps the verified retrieval calls (login with token reuse and refresh, search with filters, segment details, clip URL, neighbouring segments). Rules: timeouts on every call, retries with backoff for transient errors, clear exceptions, no secrets in logs.
Create a function find_candidates(queries, cameras, per_query_limit) that:
•   runs these queries across the available cameras (one search per camera_id so every pack is represented): "person close to a moving vehicle", "pedestrian crossing in front of a vehicle", "vehicle braking hard", "forklift near a person", "person walking in a vehicle lane";
•   de-duplicates by segment id, merges scores, and keeps the best-scoring 40 candidates overall with at most 12 per camera;
•   returns a normalised Candidate object: segment_id, video_id, camera_id, location, start, end, description, detections (class, count, boxes if available), search_score, clip_url.
Verify: a script scripts/smoke_candidates.py prints, for each camera, the number of candidates and the top hit's description. At least three cameras must return hits.
Stage 2: Severity ranking with the LLM (time-box 45 minutes)
Create backend/ranking.py. For each candidate, call the W&B inference model with a strict-JSON prompt (use JSON mode or schema if supported, otherwise validate and retry once with a repair instruction). Limit concurrency to 4 and keep total requests low (batch several candidates per call only if the output stays reliable).
Use this rubric in the prompt. Score 1 to 5:
•   1 = no real interaction (person and vehicle far apart, or no person).
•   2 = same scene, no close approach.
•   3 = close approach (a person within about one vehicle length of a moving vehicle) with no evasive action.
•   4 = close approach with evasive action (braking, swerving, stepping back), or a vulnerable road user in the vehicle's path.
•   5 = apparent contact or an extreme near-miss.
In a warehouse, treat a forklift as the moving vehicle and a person in the aisle or walkway as the pedestrian. Use ONLY the segment description and detections provided; if the evidence is weak, lower the score and say so. Never invent details.
Required JSON output per candidate:
{"segment_id": "...", "severity": 1-5, "category": "highway|driving|street|warehouse|indoor|other",
 "rationale": "one or two sentences citing the evidence",
 "people_visible": true|false, "faces_or_plates_possibly_visible": true|false,
 "recommended_action": "short, practical", "confidence": "low|medium|high"}
Keep candidates with severity of at least 3 (if fewer than 6 remain, keep the top 6 by severity then score). Sort by severity, then confidence, then search_score.
Verify: scripts/smoke_ranking.py ranks 10 real candidates and prints a table (segment, camera, severity, rationale). Check that outputs are valid JSON and the ordering is sensible; fix the prompt if not.
Stage 3: Evidence assembly (time-box 30 minutes)
Create backend/evidence.py. For each kept hit build:
•   triptych: the previous segment, the event segment and the next segment of the same video (clip URLs), each about the segment length. If a neighbour does not exist, omit it and set a flag.
•   crop: if Stage 0 proved frames and boxes are available, one image crop of the most relevant detected person or vehicle; otherwise omit.
•   privacy: a privacy_flags list built from the LLM fields and detections (for example "people visible", "faces or licence plates possibly visible"), plus the fixed note: "Review before sharing. Possible personal data (GDPR)."
Store results in memory with a JSON snapshot in data/last_run.json so the UI can reload without re-running.
Verify: scripts/smoke_evidence.py prints, for the top 3 hits, the three clip URLs and confirms each URL responds (HTTP 200 with a video content type).
Stage 4: Reports, German version, ticket (time-box 30 minutes)
Create backend/reports.py with three functions:
1.  incident_report(hit): English report with sections: Summary (2 sentences), What the footage shows, Severity and why, Location and camera, Time window, Privacy review items, Recommended actions. Must cite the segment id and time range, and only use provided evidence.
2.  translate_report(report, target="de-DE"): German version via the LLM. Use formal "Sie", metric units, 24-hour time, DD.MM.YYYY dates. Keep segment ids and numbers unchanged. Add the line "Zur manuellen Prüfung; keine Rechtsberatung."
3.  ticket_payload(hit, report): JSON with title, severity, camera_id, location, time_window, summary, clip_urls, privacy_flags, assignee (null), created_at. If the env var TICKET_WEBHOOK_URL exists, POST it when the user clicks "Create ticket"; otherwise return the JSON for download. Never fail silently.
If the corpus has speech and the Stage 0 audio check passed, offer an optional Canary-1B transcript for the event segment; otherwise skip it. Do not spend time on this unless everything else is done.
Verify: scripts/smoke_reports.py generates English and German reports for one real hit and prints both.
Stage 5: API and background jobs (time-box 30 minutes)
Endpoints (all under a router so they work behind /app):
•   POST api/investigate body {query?: string, cameras?: string[], max_hits?: number} starts a background job and returns {job_id}.
•   GET api/jobs/{job_id} returns {status, steps: [string], hits: [...]} so the UI can show live progress (steps such as "Searching camera i24_cam-1", "Ranking 38 candidates", "Assembling evidence").
•   GET api/hits/{segment_id} returns one hit.
•   POST api/hits/{segment_id}/report?lang=en|de returns the report.
•   POST api/hits/{segment_id}/ticket creates or returns the ticket payload.
•   GET api/health returns status and which optional features are available (crop, audio, webhook).
Use a simple in-memory job store. Handle errors by returning a JSON {error, hint} with the right status code.
Verify: with curl against localhost, start an investigation, poll until done, then request one report in each language and one ticket.
Stage 6: Front end (time-box 45 minutes)
One page, frontend/index.html plus app.js and styles.css, served by FastAPI at /. Requirements:
•   A header with the title, a short sentence on what the tool does, and a status line (indexed segments, last run time).
•   One primary button: "Investigate" with an optional text box for a custom query (default: "person close to a moving vehicle").
•   A live step list while the job runs.
•   Results grouped by camera / location, sorted by severity. Each card shows: severity badge with colour and text, one-line rationale, camera and time window, the triptych as three small video players labelled Before, Event, After (the event clip highlighted), the object crop if available, and privacy flags as a visible amber note.
•   Buttons per card: "Report (English)", "Bericht (Deutsch)", "Create ticket". Show results in a panel with a copy button and a download button.
•   Plain, readable design: system font, high contrast, responsive on a laptop screen and on a phone, visible keyboard focus. No external CDN dependencies.
•   Friendly empty and error states that say what happened and what to do.
Verify: run it, click through the whole flow once, and take note of anything broken. Fix before deploying.
Stage 7: Deploy and submit (time-box 45 minutes)
1.  Read deployment/deploy-app-no-registry fully and follow it exactly: Kubernetes, my team namespace, no Docker build or push, Ingress path /app on my team host.
2.  After deploying, open $INGRESS_URL path /app, run one full investigation on the deployed app, and confirm clips play.
3.  Write README.md (what it does, how it works, architecture in 8 lines, how to run, limits and known gaps) and a one-slide architecture sketch as a text diagram inside it.
4.  Run the submission skill ("help me submit our project"), which writes SUBMISSION.md. Make sure it states: the problem, the data used (packs and cameras), how search and the LLM are used, what is real versus stubbed, and the repo link.
5.  Push the repo and give me the link to paste into the submission.
6. Architecture to implement
Browser (static page, relative URLs)
   -> FastAPI (/app)
        investigate job -> vss.py (retrieval skills' API: login, search, videos)
                        -> ranking.py (W&B inference, strict JSON)
                        -> evidence.py (neighbour clips, crops, privacy flags)
        reports.py (English report, German translation, ticket payload)
VAST VSS archive (already running): search, segment details, clips, detections
7. Code quality requirements
•   Type hints and short docstrings on public functions; no dead code; one config module reading environment variables with clear errors if a required one is missing.
•   Every external call has a timeout, error handling and a log line (without secrets).
•   Pin dependencies in requirements.txt. Include a few pytest tests for pure logic: candidate merging and de-duplication, JSON validation and repair, severity sorting, German report formatting helpers.
•   Keep files short and focused; the whole project should be readable in ten minutes.
8. Acceptance checklist (all must pass before you say you are done)
•   ☐ Stage 0 findings recorded in NOTES.md.
•   ☐ Candidates returned from at least three different cameras.
•   ☐ At least 6 ranked hits, each with severity, rationale and privacy flags.
•   ☐ Top hits show three playable clips (or a clear note when a neighbour is missing).
•   ☐ English and German reports generated for a real hit, citing segment id and time range.
•   ☐ Ticket payload generated (and posted if a webhook is configured).
•   ☐ Deployed at /app on my team host and verified by running an investigation there.
•   ☐ No secrets in the repo; .gitignore in place.
•   ☐ README.md, NOTES.md, PROGRESS.md and SUBMISSION.md exist and are accurate.
•   ☐ Unit tests pass.
9. Fallbacks (apply instead of stalling)
•   W&B inference unavailable or unreliable: rank with a rule-based score (search_score, number of detected persons and vehicles, words such as braking, swerving, close, near in the description) and generate reports from templates; tell me clearly this is the fallback.
•   No frame or crop available: skip crops, keep clips.
•   Neighbouring segments unavailable: show the event clip only and label it.
•   Search returns nothing: check login, then the dashboard, then valid metadata values (retrieval/list-metadata) before changing queries.
•   Deployment problems: keep running locally on the VM for the demo and tell me so I can show it from the VM.
•   A camera not yet indexed (for example San Francisco): skip it and say so in the UI.
10. Scope cuts, in this order, if time runs short
1.  Canary audio features (do not start them unless all else is done).
2.  Object crops.
3.  German translation (keep the English report).
4.  Ticket webhook (keep the downloadable payload).
5.  Custom query box (keep the default investigation).
6.  Never cut: ranking, triptych or event clip, privacy flags, deployment, submission.


