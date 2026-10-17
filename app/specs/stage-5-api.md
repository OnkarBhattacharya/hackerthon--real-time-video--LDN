# Stage 5 — API and background jobs

**Time box:** 30 minutes
**Depends on:** Stages 1–4 callable without the UI
**Blocks:** the frontend, which uses only these endpoints

Shared rules: [constraints.md](constraints.md).

## Outcome

A FastAPI app starts an investigation in the background, reports progress, and serves one hit, either report language, and a ticket. Routes live on a router so the same app works at `/` locally and behind the ingress prefix `/app`.

## Router

Mount the router at `/api`. Do not hard-code the host or the `/app` prefix inside handlers. Relative URLs from the browser must keep working when the ingress rewrites `/app` to `/`.

| Method | Path | Body / query | Response |
| --- | --- | --- | --- |
| `POST` | `/api/investigate` | `{query?: string, cameras?: string[], max_hits?: number}` | `{job_id}` |
| `GET` | `/api/jobs/{job_id}` | — | `{status, steps: [string], hits: [...]}` |
| `GET` | `/api/hits/{segment_id}` | — | one evidence hit |
| `POST` | `/api/hits/{segment_id}/report` | `lang=en` or `lang=de` | the report |
| `POST` | `/api/hits/{segment_id}/ticket` | — | the ticket payload, posted when configured |
| `GET` | `/api/health` | — | status and which optional features are available |

Defaults:

- `query` defaults to `person close to a moving vehicle`. The other Stage 1 queries still run; this value is the extra user query, not a replacement for the whole set, unless the operator's custom text is non-empty, in which case it is **added** to the default query list.
- `cameras` defaults to every indexed camera from Stage 0, excluding cameras noted as not indexed.
- `max_hits` defaults to the Stage 2 keep rule (severity at least 3, minimum 6, capped by the candidate pool).

`status` is `queued`, `running`, `succeeded`, or `failed`.

`steps` is an append-only list of human-readable lines written as the job advances. Examples:

- `Searching camera i24_cam-1`
- `Ranking 38 candidates`
- `Assembling evidence`

Use the real camera id and the real candidate count. Hits stay empty until evidence assembly finishes, then contain the Stage 3 objects. A failed job keeps the steps it completed and adds an error step.

## Job store

In memory is enough. One process, no database.

- `job_id` is a new UUID.
- The investigate function runs off the request thread.
- The pipeline order is fixed: `find_candidates` → `rank_candidates` → `assemble_evidence` → `save_snapshot`.
- Unknown `job_id` returns 404.
- Unknown `segment_id` returns 404.
- `lang` other than `en` or `de` returns 400.

## Errors

Every error response is JSON:

```json
{"error": "what happened", "hint": "what to try next"}
```

Use the matching HTTP status. Webhook failures from Stage 4 use 502 and a hint that names the failure without the webhook URL or secret. Missing configuration uses 500 and names the variable. Do not return an empty body or an HTML error page.

## Health

```json
{
  "status": "ok",
  "features": {
    "crop": false,
    "audio": false,
    "webhook": false
  }
}
```

`crop` and `audio` reflect the Stage 0 decisions. `webhook` is true only when `TICKET_WEBHOOK_URL` is set. `status` is `ok` when login configuration is present, even if optional features are off.

## Static files

Serve `frontend/` at `/` from the same app. Stage 6 owns the page contents. This stage only mounts the directory if it exists, so the API smoke test can run before the page is finished.

## Verification

Against `localhost`, with curl:

1. `GET /api/health` returns the feature flags.
2. `POST /api/investigate` returns a `job_id`.
3. Poll `GET /api/jobs/{job_id}` until `succeeded` or `failed`. A failure stops the stage; fix the pipeline before continuing.
4. `GET /api/hits/{segment_id}` for the first hit.
5. `POST /api/hits/{segment_id}/report?lang=en`
6. `POST /api/hits/{segment_id}/report?lang=de`
7. `POST /api/hits/{segment_id}/ticket` and confirm either a posted result or a downloadable payload, matching Stage 4.

Also request an unknown job id and confirm `{error, hint}` with 404.

## Done when

- The curl sequence completes on a real investigation.
- `PROGRESS.md` records the job id, hit count, and feature flags from `/api/health`.
