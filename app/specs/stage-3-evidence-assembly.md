# Stage 3 — Evidence assembly

**Time box:** 30 minutes
**Depends on:** Stage 2 kept hits, and the Stage 0 notes for neighbour clips, frames, and boxes
**Blocks:** reports and the results UI

Shared rules: [constraints.md](constraints.md).

## Outcome

Each kept hit gains a before/event/after clip triptych, an optional crop, and privacy flags. A JSON snapshot lets the UI reload the last run without calling VSS or the LLM again.

## API

```python
def assemble_evidence(hits: list[RankedHit]) -> list[EvidenceHit]:
    """Attach neighbour clips, an optional crop, and privacy flags."""
```

```python
def save_snapshot(hits: list[EvidenceHit], path: Path = Path("data/last_run.json")) -> None:
    """Write the latest successful run so the API can reload it."""
```

```python
def load_snapshot(path: Path = Path("data/last_run.json")) -> list[EvidenceHit] | None:
    """Return the last run, or None when no snapshot exists."""
```

## Triptych

For each hit, resolve three clips on the **same video**:

| Slot | Segment | UI label |
| --- | --- | --- |
| `previous` | previous neighbour | Before |
| `event` | the ranked segment | Event |
| `next` | next neighbour | After |

Each clip URL is the playable URL from the Stage 0 videos contract. Clip length is the segment's own length. Do not re-cut with FFmpeg.

When a neighbour does not exist, omit that slot and set `missing_neighbours` to the list of missing roles (`previous`, `next`, or both). The event clip is required. If the event clip URL cannot be resolved, the hit is an error result with a hint, not a silent skip.

## Crop

If Stage 0 recorded that a frame and bounding boxes are available, attach one crop of the most relevant detected person or vehicle:

- prefer the person box when a person and a vehicle are both present
- otherwise the vehicle or forklift box
- store `crop_url` or an inline path the API can serve

If Stage 0 marked frames or boxes unavailable, omit `crop` and set `crop_available: false`. Do not spend this stage building a frame pipeline.

## Privacy

Build `privacy_flags` from the ranking fields and detections. Include a flag only when the evidence supports it:

- `people visible` when `people_visible` is true or a person class is detected
- `faces or licence plates possibly visible` when `faces_or_plates_possibly_visible` is true

Always include this fixed note as `privacy_note`, separate from the flags:

`Review before sharing. Possible personal data (GDPR).`

## Snapshot

`data/last_run.json` holds the evidence hits plus `generated_at` and the query and cameras used. The directory is created if needed. The file is gitignored. A failed run must not overwrite the previous good snapshot.

## Verification

`scripts/smoke_evidence.py` loads the top three ranked hits, assembles evidence, and for each hit prints the clip URLs it actually has. For every URL, it performs an HTTP GET with a timeout and requires HTTP 200 and a video content type.

Pass condition: the event URL passes for each of the three hits. A missing neighbour is a pass when `missing_neighbours` names it and no URL is printed for that slot. A non-video response is a failure; check the Stage 0 clip call before changing the UI.

## Done when

- The smoke script confirms the event clips.
- `data/last_run.json` reloads to the same segment ids.
- `PROGRESS.md` records whether crops were included or omitted, and any hits missing a neighbour.
