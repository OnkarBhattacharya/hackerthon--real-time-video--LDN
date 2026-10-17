"""Clip URL helpers shared by the live client and the fixture."""

from __future__ import annotations

from urllib.parse import quote


def clip_url_for_source(source: str | None) -> str | None:
    """Return a relative playable URL, or None when no source was provided."""
    if not source:
        return None
    if source.startswith("fixture://"):
        return "api/fixture/sample.mp4"
    return "api/stream?source=" + quote(source, safe="")
