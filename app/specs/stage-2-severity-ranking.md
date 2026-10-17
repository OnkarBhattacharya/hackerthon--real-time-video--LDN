# Stage 2 — Severity ranking with the LLM

**Time box:** 45 minutes
**Depends on:** Stage 1 `Candidate` objects and the Stage 0 W&B notes (base URL, model name, JSON behaviour)
**Blocks:** evidence assembly, which only sees kept hits

Shared rules: [constraints.md](constraints.md).

## Outcome

`backend/ranking.py` scores each candidate from 1 to 5 with a strict JSON rationale, drops weak interactions, and returns a stable sort. The model may only use the description and detections it is given.

## API

```python
def rank_candidates(candidates: list[Candidate]) -> list[RankedHit]:
    """Score candidates and return those kept, sorted for the UI."""
```

A `RankedHit` is the candidate plus:

```json
{
  "segment_id": "...",
  "severity": 3,
  "category": "warehouse",
  "rationale": "One or two sentences citing the evidence.",
  "people_visible": true,
  "faces_or_plates_possibly_visible": false,
  "recommended_action": "Short, practical.",
  "confidence": "medium"
}
```

`category` is one of `highway`, `driving`, `street`, `warehouse`, `indoor`, `other`.
`confidence` is one of `low`, `medium`, `high`.
`severity` is an integer from 1 to 5.

## Rubric

Put this rubric in the prompt unchanged in meaning:

- **1** — no real interaction. The person and vehicle are far apart, or there is no person.
- **2** — same scene, no close approach.
- **3** — close approach. A person is within about one vehicle length of a moving vehicle, with no evasive action.
- **4** — close approach with evasive action (braking, swerving, stepping back), or a vulnerable road user in the vehicle's path.
- **5** — apparent contact or an extreme near-miss.

In a warehouse, a forklift is the moving vehicle and a person in the aisle or walkway is the pedestrian.

The prompt must say:

- use only the segment description and detections provided
- if the evidence is weak, lower the score and say so in the rationale
- never invent objects, motion, or contact that are not in the evidence

## Calling the model

- Use the model and base URL recorded in Stage 0.
- Prefer JSON mode or a response schema when Stage 0 showed it works. Otherwise ask for a single JSON object and parse it.
- Validate every object against the schema above. On failure, retry that call **once** with a repair instruction that includes the invalid text and the schema. A second failure uses the rule-based fallback in [constraints.md](constraints.md) for that candidate, with `confidence: "low"` and a rationale that says the score is rule-based.
- Limit in-flight requests to 4.
- Keep the total request count low. Batching several candidates in one call is allowed only when a smoke run shows every object still validates. If a batch parse fails, fall back to one candidate per call rather than loosening validation.
- Timeout, log, and do not include the API key in the log line.

## Keep and sort

- Keep hits with `severity >= 3`.
- If fewer than 6 remain, keep the top 6 by severity, then `search_score`.
- Sort kept hits by severity descending, then confidence (`high`, then `medium`, then `low`), then `search_score` descending.

## Tests

`tests/test_ranking.py`, no network:

- valid JSON is accepted
- one malformed object is repaired by the validator's single repair path when the repair function returns valid JSON
- a still-invalid object is rejected
- sort order is severity, then confidence, then `search_score`
- the "fewer than 6" rule keeps six when enough lower-severity candidates exist
- the rule-based fallback score increases when the description contains a close-approach term and both a person and a vehicle are detected, and stays low when no person is detected

## Verification

`scripts/smoke_ranking.py` ranks 10 real candidates from Stage 1 and prints a table: segment id, camera, severity, rationale.

Pass condition: every row is schema-valid, severities are integers 1–5, and a reader can see why a higher row outranks a lower one. If rationales cite facts that are not in the description or detections, tighten the prompt and rerun. Do not change the rubric to fit a bad completion.

## Done when

- The smoke table is valid and the order matches the sort rules.
- `PROGRESS.md` records the model name and whether JSON mode, plain JSON, or the rule fallback was used.
