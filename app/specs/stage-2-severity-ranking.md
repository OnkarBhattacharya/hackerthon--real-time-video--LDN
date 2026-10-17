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

## Working-tree notes preserved during merge

These notes were already in the working tree when `origin/main` added this file. They record the Stage 1–3 implementation contracts and are kept alongside the spec above.

# Stage 2 — Severity ranking with the LLM (spec)

**Time-box:** 45 minutes  
**Depends on:** Stage 1 `Candidate` + `find_candidates`  
**Goal:** Score near-miss candidates 1–5 via W&B serverless inference with strict JSON; keep actionable hits.

## In scope

| Deliverable | Description |
|-------------|-------------|
| `backend/config.py` | Add W&B inference settings (`WANDB_API_KEY`, optional model/base URL) |
| `backend/models.py` | `RankedHit` extending candidate + severity fields |
| `backend/llm.py` | Thin OpenAI-compatible chat client for W&B Inference |
| `backend/ranking.py` | Rubric prompt, JSON validate/repair, concurrency ≤4, filter+sort |
| `scripts/smoke_ranking.py` | Rank 10 real candidates; print table |

## Inference

- Base URL: `WANDB_INFERENCE_URL` or `https://api.inference.wandb.ai/v1`
- Auth: `Authorization: Bearer $WANDB_API_KEY`
- Default model: `WANDB_MODEL` or `openai/gpt-oss-20b`
- Prefer `response_format: {type: json_object}`; on parse failure, one repair retry

## Rubric (prompt)

1 = no real interaction · 2 = same scene, no close approach · 3 = close approach (~1 vehicle length), no evasive action · 4 = close approach + evasive action or VRU in path · 5 = contact / extreme near-miss. Warehouse: forklift = vehicle. Use only provided evidence.

## Required JSON per candidate

```json
{
  "segment_id": "...",
  "severity": 1,
  "category": "highway|driving|street|warehouse|indoor|other",
  "rationale": "...",
  "people_visible": true,
  "faces_or_plates_possibly_visible": false,
  "recommended_action": "...",
  "confidence": "low|medium|high"
}
```

## Selection

- Keep severity ≥ 3; if fewer than 6 remain, keep top 6 by severity then search_score.
- Sort: severity DESC, confidence (high>medium>low), search_score DESC.

## Fallback

If W&B unavailable: rule-based score from search_score + person/vehicle detection counts + keywords (braking, swerving, close, near, forklift). Mark `confidence=low` and log clearly.

## Verify

```bash
python scripts/smoke_ranking.py
```

Prints a table of 10 ranked rows with valid JSON-derived fields and sensible ordering.
