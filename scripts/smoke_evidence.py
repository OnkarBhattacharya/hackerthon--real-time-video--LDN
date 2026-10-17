#!/usr/bin/env python3
"""Stage 3 verify: triptych URLs for the top 3 hits respond as video."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from backend.candidates import find_candidates  # noqa: E402
from backend.evidence import GDPR_NOTE, assemble_evidence  # noqa: E402
from backend.ranking import rank_candidates  # noqa: E402
from backend.vss import VssClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SMOKE_CAMERAS = [
    "neighborhood_cam-1",
    "nyc_streets_cam-1",
    "sdg_warehouse_cam-2",
    "sf_streets_cam-2",
    "pie_cam-3",
]
RANK_LIMIT = 8
CHECK_TOP = 3
_TOKEN = re.compile(r"([?&]token=)[^&]+")


def redact_url(url: str) -> str:
    """Hide the JWT query param before printing a clip URL."""
    return _TOKEN.sub(r"\1***", url)


def verify_clip(url: str) -> tuple[int, str, bool]:
    """Return HTTP status, content type, and whether the body is a playable video.

    The live stream endpoint returns ``binary/octet-stream`` with an MP4 body.
    A ``video/*`` content type is also accepted. Exceptions are reported by
    type name only so the token in the URL is not logged.
    """
    try:
        response = requests.get(url, stream=True, timeout=45)
    except requests.RequestException as exc:
        return 0, type(exc).__name__, False
    try:
        ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        chunk = next(response.iter_content(32), b"")
        is_mp4 = b"ftyp" in chunk[:16]
        ok = response.status_code == 200 and (ctype.startswith("video/") or is_mp4)
        return response.status_code, ctype or "-", ok
    finally:
        response.close()


def _cell(present: bool, result: tuple[int, str, bool] | None) -> str:
    if not present:
        return "missing"
    if result is None:
        return "no-url"
    status, _ctype, ok = result
    return f"{status} {'mp4' if ok else 'not-video'}"


def main() -> int:
    client = VssClient()
    client.login()
    candidates = find_candidates(
        client=client,
        cameras=SMOKE_CAMERAS,
        per_query_limit=2,
    )
    sample = candidates[:RANK_LIMIT]
    print(f"Ranking {len(sample)} of {len(candidates)} candidates…")
    if not sample:
        print("FAIL: no candidates", file=sys.stderr)
        return 1
    ranked = rank_candidates(sample)
    if not ranked:
        print("FAIL: no ranked hits", file=sys.stderr)
        return 1
    bundles = assemble_evidence(ranked, client=client)
    top = bundles[:CHECK_TOP]

    print()
    print(f"{'sev':>3}  {'camera':<22}  {'before':<14}  {'event':<14}  {'after':<14}")
    print("-" * 76)
    failed = False
    for bundle in top:
        slots = bundle.triptych
        checks: dict[str, tuple[int, str, bool] | None] = {}
        for role in ("previous", "event", "next"):
            slot = slots[role]
            if not slot.present or not slot.clip_url:
                checks[role] = None
                continue
            checks[role] = verify_clip(slot.clip_url)
            status, _ctype, ok = checks[role]
            if not ok:
                failed = True
        print(
            f"{bundle.hit.severity:>3}  {bundle.hit.candidate.camera_id:<22}  "
            f"{_cell(slots['previous'].present, checks['previous']):<14}  "
            f"{_cell(slots['event'].present, checks['event']):<14}  "
            f"{_cell(slots['next'].present, checks['next']):<14}"
        )
        for role, label in (("previous", "before"), ("event", "event"), ("next", "after")):
            slot = slots[role]
            if not slot.present or not slot.clip_url:
                print(f"     {label:<6}  omitted ({role})")
                continue
            status, ctype, ok = checks[role] or (0, "-", False)
            print(
                f"     {label:<6}  HTTP {status}  {ctype}  "
                f"{'video' if ok else 'FAIL'}  {redact_url(slot.clip_url)}"
            )
        if GDPR_NOTE not in bundle.privacy_flags:
            print("FAIL: missing GDPR note", file=sys.stderr)
            failed = True
        if bundle.crop_available or not bundle.crop_omitted:
            print("FAIL: crop should be omitted", file=sys.stderr)
            failed = True

    print("-" * 76)
    print(
        f"kept={len(bundles)} checked={len(top)} "
        f"crop_available={bundles[0].crop_available}"
    )
    if failed or not top:
        print("FAIL")
        return 1
    if any(not b.triptych["event"].present for b in top):
        print("FAIL: event clip missing")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
