#!/usr/bin/env python3
"""Stage 2 verify: rank real candidates and print a severity table."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.candidates import DEFAULT_QUERIES, find_candidates  # noqa: E402
from backend.ranking import rank_candidates  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SMOKE_CAMERAS = [
    "neighborhood_cam-1",
    "nyc_streets_cam-1",
    "sdg_warehouse_cam-2",
    "sf_streets_cam-2",
    "pie_cam-3",
]


def main() -> int:
    candidates = find_candidates(list(DEFAULT_QUERIES), SMOKE_CAMERAS, 4)
    if len(candidates) < 10:
        print(f"WARN: only {len(candidates)} candidates; continuing", file=sys.stderr)
    sample = candidates[:10]
    print(f"Ranking {len(sample)} candidates…")
    ranked = rank_candidates(sample)

    print()
    print(f"{'sev':>3}  {'conf':<6}  {'cam':<22}  {'src':<8}  rationale")
    print("-" * 100)
    prev_sev = 99
    order_ok = True
    for hit in ranked:
        if hit.severity > prev_sev:
            order_ok = False
        prev_sev = hit.severity
        seg = hit.candidate.segment_id.rsplit("/", 1)[-1][:28]
        rationale = (hit.rationale or "").replace("\n", " ")[:70]
        source = "rules" if hit.rule_based else "llm"
        print(
            f"{hit.severity:>3}  {hit.confidence:<6}  {hit.candidate.camera_id:<22}  "
            f"{source:<8}  {rationale}"
        )
        print(f"     segment={seg} category={hit.category}")

    llm_n = sum(1 for hit in ranked if not hit.rule_based)
    print("-" * 100)
    print(f"kept={len(ranked)} llm={llm_n} fallback={len(ranked) - llm_n}")

    if not ranked:
        print("FAIL: no ranked hits", file=sys.stderr)
        return 1
    for hit in ranked:
        if not (1 <= hit.severity <= 5) or hit.confidence not in {"low", "medium", "high"}:
            print("FAIL: invalid fields", file=sys.stderr)
            return 1
        if not hit.rationale:
            print("FAIL: empty rationale", file=sys.stderr)
            return 1
    if not order_ok:
        print("FAIL: not sorted by severity descending", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
