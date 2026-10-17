#!/usr/bin/env python3
"""Stage 3 verify: triptych clips for the top hits respond as video."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.candidates import DEFAULT_QUERIES, find_candidates  # noqa: E402
from backend.evidence import assemble_evidence  # noqa: E402
from backend.models import PRIVACY_NOTE  # noqa: E402
from backend.ranking import rank_candidates  # noqa: E402
from backend.vss import VssError, get_client  # noqa: E402

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


def source_of(clip_url: str) -> str | None:
    """Read the stream source out of a relative clip URL."""
    parsed = urlparse(clip_url if "://" in clip_url else "http://local/" + clip_url)
    values = parse_qs(parsed.query).get("source") or []
    return values[0] if values else None


def redact_url(url: str) -> str:
    """Hide a token query param before printing a clip URL."""
    if "token=" not in url:
        return url
    return url.split("token=", 1)[0] + "token=***"


def verify_clip(clip_url: str) -> tuple[int, str, bool]:
    """Return HTTP status, content type, and whether the body looks like video."""
    source = source_of(clip_url)
    if not source:
        return 0, "no-source", False
    try:
        response = get_client().open_stream(source, None)
    except VssError:
        return 0, "error", False
    try:
        ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        chunk = next(response.iter_bytes(32), b"")
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
    candidates = find_candidates(list(DEFAULT_QUERIES), SMOKE_CAMERAS, 2)
    sample = candidates[:RANK_LIMIT]
    print(f"Ranking {len(sample)} of {len(candidates)} candidates…")
    if not sample:
        print("FAIL: no candidates", file=sys.stderr)
        return 1
    ranked = rank_candidates(sample)
    if not ranked:
        print("FAIL: no ranked hits", file=sys.stderr)
        return 1
    hits = assemble_evidence(ranked)
    top = hits[:CHECK_TOP]

    print()
    print(f"{'sev':>3}  {'camera':<22}  {'before':<14}  {'event':<14}  {'after':<14}")
    print("-" * 76)
    failed = False
    for hit in top:
        checks: dict[str, tuple[int, str, bool] | None] = {}
        present: dict[str, bool] = {}
        for role in ("previous", "event", "next"):
            clip = hit.clips.get(role)
            clip_url = clip.get("clip_url") if isinstance(clip, dict) else None
            present[role] = bool(clip_url)
            if not clip_url:
                checks[role] = None
                continue
            checks[role] = verify_clip(str(clip_url))
            _status, _ctype, ok = checks[role]
            if not ok:
                failed = True
        print(
            f"{hit.ranked.severity:>3}  {hit.ranked.candidate.camera_id:<22}  "
            f"{_cell(present['previous'], checks['previous']):<14}  "
            f"{_cell(present['event'], checks['event']):<14}  "
            f"{_cell(present['next'], checks['next']):<14}"
        )
        for role, label in (("previous", "before"), ("event", "event"), ("next", "after")):
            clip = hit.clips.get(role)
            clip_url = clip.get("clip_url") if isinstance(clip, dict) else None
            if not clip_url:
                print(f"     {label:<6}  omitted ({role})")
                continue
            status, ctype, ok = checks[role] or (0, "-", False)
            print(
                f"     {label:<6}  HTTP {status}  {ctype}  "
                f"{'video' if ok else 'FAIL'}  {redact_url(str(clip_url))}"
            )
        if PRIVACY_NOTE not in hit.privacy_note:
            print("FAIL: missing GDPR note", file=sys.stderr)
            failed = True
        if hit.crop_available:
            print("FAIL: crop should be omitted", file=sys.stderr)
            failed = True

    print("-" * 76)
    print(f"kept={len(hits)} checked={len(top)} crop_available={hits[0].crop_available if hits else None}")
    if failed or not top:
        print("FAIL")
        return 1
    if any(not hit.clips.get("event") for hit in top):
        print("FAIL: event clip missing")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
