#!/usr/bin/env python3
"""Stage 1 verify: print candidate counts per camera and top descriptions."""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.candidates import DEFAULT_QUERIES, find_candidates  # noqa: E402
from backend.vss import get_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    cameras = get_client().list_cameras()
    print(f"cameras ({len(cameras)}): {', '.join(cameras)}")

    candidates = find_candidates(list(DEFAULT_QUERIES), cameras, 6)
    by_cam: dict[str, list] = defaultdict(list)
    for candidate in candidates:
        by_cam[candidate.camera_id].append(candidate)

    print(f"\nTotal candidates (capped): {len(candidates)}")
    print("-" * 72)
    cameras_with_hits = 0
    for cam in sorted(by_cam):
        items = sorted(by_cam[cam], key=lambda item: item.search_score, reverse=True)
        cameras_with_hits += 1
        top = items[0]
        desc = (top.description or "").replace("\n", " ")[:140]
        print(f"{cam}: {len(items)} candidates")
        print(f"  top score={top.search_score:.3f} loc={top.location!r}")
        print(f"  {desc}")
        print()

    print("-" * 72)
    print(f"cameras_with_hits={cameras_with_hits} (need >= 3)")
    if cameras_with_hits < 3:
        print("FAIL: fewer than 3 cameras returned hits", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
