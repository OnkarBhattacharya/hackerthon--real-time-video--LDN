#!/usr/bin/env python3
"""Stage 4 verify: English and German reports for one real evidence hit."""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.candidates import find_candidates  # noqa: E402
from backend.config import load_settings  # noqa: E402
from backend.evidence import assemble_evidence  # noqa: E402
from backend.ranking import rank_candidates  # noqa: E402
from backend.reports import (  # noqa: E402
    GERMAN_DISCLAIMER,
    ReportError,
    TicketWebhookError,
    format_offset,
    incident_report,
    ticket_payload,
    translate_report,
)
from backend.vss import VssClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SMOKE_CAMERAS = [
    "neighborhood_cam-1",
    "nyc_streets_cam-1",
    "sdg_warehouse_cam-2",
    "sf_streets_cam-2",
    "pie_cam-3",
]
RANK_LIMIT = 4
_TICKET_KEYS = (
    "title",
    "severity",
    "camera_id",
    "location",
    "time_window",
    "summary",
    "clip_urls",
    "privacy_flags",
    "assignee",
    "created_at",
)


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
    snapshot = Path(tempfile.mkdtemp(prefix="smoke-reports-")) / "run.json"
    bundles = assemble_evidence(ranked[:1], client=client, snapshot_path=snapshot)
    if not bundles:
        print("FAIL: no evidence hit", file=sys.stderr)
        return 1
    bundle = bundles[0]
    segment_id = bundle.hit.segment_id
    start = format_offset(bundle.hit.candidate.start)
    end = format_offset(bundle.hit.candidate.end)

    english = incident_report(bundle)
    print("\n===== English report =====\n")
    print(english.text)
    try:
        german = translate_report(english)
    except ReportError as exc:
        print(f"FAIL: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("\n===== German report =====\n")
    print(german.text)

    failed = False
    if not english.text.strip() or not german.text.strip():
        print("FAIL: empty report", file=sys.stderr)
        failed = True
    if segment_id not in english.text or segment_id not in german.text:
        print("FAIL: segment id missing", file=sys.stderr)
        failed = True
    if start not in english.text or start not in german.text:
        print("FAIL: start offset missing", file=sys.stderr)
        failed = True
    if end not in english.text or end not in german.text:
        print("FAIL: end offset missing", file=sys.stderr)
        failed = True
    if german.text.count(GERMAN_DISCLAIMER) != 1:
        print("FAIL: German disclaimer missing or repeated", file=sys.stderr)
        failed = True
    if "token=" in english.text or "token=" in german.text:
        print("FAIL: report contains a token", file=sys.stderr)
        failed = True

    configured = bool(load_settings().ticket_webhook_url)
    try:
        payload = ticket_payload(bundle, english)
    except TicketWebhookError as exc:
        print(f"FAIL: webhook {exc}", file=sys.stderr)
        return 1
    missing = [key for key in _TICKET_KEYS if key not in payload]
    if missing:
        print(f"FAIL: ticket missing {missing}", file=sys.stderr)
        failed = True
    if payload.get("assignee") is not None:
        print("FAIL: assignee is not null", file=sys.stderr)
        failed = True
    window = payload.get("time_window") or {}
    if window.get("segment_id") != segment_id:
        print("FAIL: ticket time window segment id", file=sys.stderr)
        failed = True

    print("\n===== Ticket =====")
    print(f"keys={', '.join(_TICKET_KEYS)}")
    print(f"assignee=null severity={payload.get('severity')} camera={payload.get('camera_id')}")
    clips = payload.get("clip_urls") or {}
    print(
        "clip_urls "
        f"previous={'set' if clips.get('previous') else 'null'} "
        f"event={'set' if clips.get('event') else 'null'} "
        f"next={'set' if clips.get('next') else 'null'}"
    )
    if configured:
        print("webhook: configured and POST completed")
    else:
        print("webhook: not configured; payload returned only")

    if failed:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
