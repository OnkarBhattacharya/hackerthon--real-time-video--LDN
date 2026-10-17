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

## Working-tree notes preserved during merge

These notes were already in the working tree when `origin/main` added this file. They record the Stage 1–3 implementation contracts and are kept alongside the spec above.

# Stage 1 — Data layer (spec)

**Time-box:** 45 minutes  
**Depends on:** Stage 0 retrieval login/search/videos working (or equivalent live VSS)  
**Goal:** A FastAPI-ready `backend/` package that finds near-miss / person–vehicle candidates across cameras via the team VSS API.

## Problem

Operations reviewers need ranked video moments where people interact closely with vehicles (street, highway, warehouse forklifts). Stage 1 only **retrieves and normalises** candidates; ranking and reports come later.

## In scope

| Deliverable | Description |
|-------------|-------------|
| `backend/config.py` | Read `INGRESS_URL` / `VSS_URL`, `USERNAME`/`VSS_USERNAME`, `PASSWORD`/`VSS_PASSWORD`; fail clearly if missing |
| `backend/models.py` | `Candidate`, `DetectionBox` (and related) typed models |
| `backend/vss.py` | Thin VSS client: login + token reuse/refresh, search with filters, segment metadata, detections, clip URL, neighbouring segments |
| `backend/candidates.py` | `find_candidates(queries, cameras, per_query_limit)` |
| `backend/main.py` | Minimal FastAPI app stub (health only for now) |
| `scripts/smoke_candidates.py` | Prints per-camera counts + top description; gate: ≥3 cameras with hits |

## Out of scope (later stages)

- LLM severity ranking, evidence triptych assembly, reports, UI, deploy

## VSS contracts used

| Operation | Endpoint |
|-----------|----------|
| Login | `POST /api/v1/auth/login` |
| Search | `POST /api/v1/search` with `metadata_filters.camera_id` |
| Segment row | `GET /api/v1/videos/metadata?source=` |
| Detections | `GET /api/v1/videos/detections?source=` (404 → empty boxes) |
| Clip URL | `{base}/api/v1/videos/stream?source=&token=` (JWT in query) |
| Neighbours | `GET /api/v1/tools/segments?original_video=` → order by `segment_number` |

Base URL inside the cluster: `http://video-backend-service:8000`. On the VM for scripts: `$INGRESS_URL`.

## Default investigation queries

1. `person close to a moving vehicle`  
2. `pedestrian crossing in front of a vehicle`  
3. `vehicle braking hard`  
4. `forklift near a person`  
5. `person walking in a vehicle lane`

## `find_candidates` rules

1. For each query × each `camera_id`, run one search (`top_k = per_query_limit`, `min_similarity` default `0.15`, `include_public=true`).
2. **Segment id** = segment `source` S3 URI (stable unique key).
3. De-dupe by segment id; when merging, keep the **max** `similarity_score`.
4. Cap **12** candidates per camera, then keep the global top **40** by score.
5. Return normalised `Candidate` objects (see fields below). Skip cameras that return zero hits without failing the run.

## `Candidate` fields

```text
segment_id      str   # source S3 URI
video_id        str   # original_video S3 URI
camera_id       str
location        str
start           float # segment_start_sec
end             float # segment_end_sec
description     str   # reasoning_content
detections      list[{class, count, boxes?}]
search_score    float
clip_url        str   # stream URL with current token
```

## Client quality rules

- Timeout on every HTTP call  
- Retries with exponential backoff on 429/5xx/connection errors (bounded)  
- Clear typed exceptions (`VssAuthError`, `VssRequestError`)  
- **Never** log password or JWT  

## Verify (gate)

```bash
python scripts/smoke_candidates.py
```

Expect: at least **three** distinct `camera_id`s with ≥1 candidate; print per-camera count and top hit description.

## Notes from live metadata (team-41)

Cameras include: `i24_cam-1`, `neighborhood_cam-1`, `nyc_bike_gopro-1`, `nyc_streets_cam-*`, `pie_cam-3`, `sdg_warehouse_cam-2`, `sf_streets_cam-*`, `smartspace_cam-1`.  
Locations: `indoor`, `nashville`, `neighborhood`, `new_york`, `san_francisco`, `toronto`, `warehouse3`.  
Capture types: `crowds`, `streets`, `traffic`, `warehouse`.
