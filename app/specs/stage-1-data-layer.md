# Stage 1 — Data layer

**Time box:** 45 minutes
**Depends on:** Stage 0 `NOTES.md` and a successful login, search, and videos check
**Blocks:** ranking, evidence, reports, and the investigate job

Shared rules: [constraints.md](constraints.md).

## Outcome

`backend/` exposes a thin VSS client and a `find_candidates` function that returns a de-duplicated, camera-balanced set of normalised near-miss candidates from the live archive.

## Modules

### `backend/config.py`

Read environment variables in one place. Required variables are whatever Stage 0 recorded for VSS login and base URL. Raise a clear error naming the missing variable. Do not log secret values.

### `backend/vss.py`

Wrap only the calls Stage 0 verified:

- login, with token reuse and refresh before expiry
- search with filters (at least `camera_id`)
- segment details and detections
- clip URL
- previous and next segments of the same video

Rules for every call:

- explicit timeout
- retries with backoff only for transient failures (timeouts, connection errors, HTTP 429 and 5xx)
- exceptions that name the operation and status, without response bodies that may contain secrets
- one log line per call: operation, camera or segment id, elapsed time, status

Do not add ingest, upload, embedding, or local detection.

### Candidate search

```python
def find_candidates(
    queries: list[str],
    cameras: list[str],
    per_query_limit: int,
) -> list[Candidate]:
    """Search each camera for each query and return the best normalised hits."""
```

Default queries, all run so every available pack is represented:

- `person close to a moving vehicle`
- `pedestrian crossing in front of a vehicle`
- `vehicle braking hard`
- `forklift near a person`
- `person walking in a vehicle lane`

Run **one search per `camera_id`**. Use the camera ids recorded in Stage 0. Skip a camera that Stage 0 showed is not indexed, and record that skip.

De-duplication:

- identity is segment id
- if the same segment matches more than one query, keep one candidate and merge scores by keeping the maximum `search_score`
- keep the best 40 candidates overall
- keep at most 12 candidates per camera

`Candidate` fields:

| Field | Source |
| --- | --- |
| `segment_id` | search / videos contract in `NOTES.md` |
| `video_id` | same |
| `camera_id` | search filter or result |
| `location` | result metadata |
| `start`, `end` | segment time fields, unchanged units |
| `description` | segment description text only |
| `detections` | list of `{class, count, boxes}`. `boxes` is `null` when Stage 0 marked boxes unavailable |
| `search_score` | search score |
| `clip_url` | the clip call from Stage 0 |

Map fields only from the Stage 0 contract. If a field is absent, leave it empty or null. Do not fill it from another segment.

## Tests

`tests/test_candidates.py` covers pure helpers, with no network:

- two hits with the same segment id collapse to one and keep the higher score
- the 12-per-camera cap is enforced
- the overall cap is 40
- ordering after the cap is by `search_score` descending

## Verification

`scripts/smoke_candidates.py` calls `find_candidates` against the live archive and prints, for each camera, the candidate count and the top hit's description.

Pass condition: at least three cameras return one or more hits. If fewer do, follow the search fallback in [constraints.md](constraints.md) before changing queries.

## Done when

- The smoke script prints at least three cameras with hits.
- `PROGRESS.md` lists cameras searched, cameras skipped, and hit counts.
