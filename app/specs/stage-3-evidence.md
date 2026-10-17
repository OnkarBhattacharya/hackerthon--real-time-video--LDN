# Stage 3 — Evidence assembly (spec)

**Time-box:** 30 minutes  
**Depends on:** Stage 2 `RankedHit` (kept, severity-sorted)  
**Goal:** For each kept hit, attach a before/event/after clip triptych and privacy flags, and snapshot the run for the UI.

## In scope

| Deliverable | Description |
|-------------|-------------|
| `backend/evidence.py` | Build triptych + privacy flags; in-memory store; JSON snapshot |
| `data/last_run.json` | Snapshot so the UI can reload without re-running (gitignored) |
| `scripts/smoke_evidence.py` | Top 3 hits: clip URLs (token redacted) and HTTP check |

## Out of scope

- Reports, tickets, API jobs, UI (Stages 4–6)
- Object crops (no downloadable frame image; see Crop)

## Triptych

For each kept `RankedHit`, call `VssClient.neighbouring_segments(video_id, segment_number)`.

| Slot | Source |
|------|--------|
| previous | neighbour `segment_number - 1` |
| event | neighbour current, else the hit's own `segment_id` |
| next | neighbour `segment_number + 1` |

Each present slot stores a fresh `clip_url` (`/api/v1/videos/stream?source=&token=`). If a neighbour row is missing or has no `source`, **omit the URL** and append `missing_previous` or `missing_next` to `neighbour_flags`. A failure of the neighbour call does not drop the event clip.

## Crop

Stage 0 / a live detections read: `GET /api/v1/videos/detections` returns `frames[].detections[].bbox` only. No frame image, thumbnail, or crop endpoint. **Omit `crop`.** Set `crop_available=false` and `crop_omitted=true` with a short reason. Do not call YOLO or ffmpeg in this stage.

## Privacy

`privacy_flags` is built from the ranked hit and detection class names:

- `people visible` when `people_visible` is true or a detection class looks like a person
- `faces or licence plates possibly visible` when `faces_or_plates_possibly_visible` is true or a class looks like a face or plate
- always ends with `Review before sharing. Possible personal data (GDPR).`

## Snapshot

`assemble_evidence` keeps the last payload in process memory (`get_last_run`) and writes `data/last_run.json` (override with `snapshot_path`). The file includes ranked fields, triptych URLs, flags, and the crop decision. Clip URLs embed a short-lived JWT. `data/` is already in `.gitignore`; do not commit the snapshot.

## Verify

```bash
python scripts/smoke_evidence.py
```

Uses the same camera subset as Stage 2 and ranks at most 8 candidates. For the top 3 hits, prints which of before/event/after exist and checks each existing URL.

Live stream responses are HTTP 200 with `Content-Type: binary/octet-stream` and an MP4 body (`ftyp`). The smoke treats that as a video, and also accepts a `video/*` content type. Tokens are redacted in printed URLs and are not logged.
