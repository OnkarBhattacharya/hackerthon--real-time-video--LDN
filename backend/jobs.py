"""In-memory investigation jobs."""

from __future__ import annotations

import copy
import logging
import threading
import uuid
from typing import Any

from backend.candidates import DEFAULT_QUERIES, find_candidates
from backend.config import ConfigError
from backend.evidence import assemble_evidence, load_snapshot, save_snapshot
from backend.ranking import rank_candidates
from backend.vss import VssError, get_client

logger = logging.getLogger(__name__)


class JobStore:
    """One-process job and hit index."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._hits: dict[str, dict[str, Any]] = {}

    def create(self) -> str:
        """Create a queued job and return its id."""
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = {
                "status": "queued",
                "steps": [],
                "hits": [],
                "error": None,
                "hint": None,
                "skipped_cameras": [],
            }
        return job_id

    def mark_running(self, job_id: str) -> None:
        """Move a job from queued to running."""
        with self._lock:
            self._jobs[job_id]["status"] = "running"

    def add_step(self, job_id: str, message: str) -> None:
        """Append a progress line."""
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "running"
            job["steps"].append(message)
            if message.startswith("Skipping camera "):
                camera_id = message.removeprefix("Skipping camera ").split(";", 1)[0].strip()
                job["skipped_cameras"].append(camera_id)

    def succeed(self, job_id: str, hits: list[dict[str, Any]]) -> None:
        """Store the finished hits."""
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "succeeded"
            job["hits"] = hits
            for hit in hits:
                self._hits[str(hit["segment_id"])] = hit

    def fail(self, job_id: str, error: str, hint: str) -> None:
        """Record a failure and keep the steps already completed."""
        with self._lock:
            job = self._jobs[job_id]
            job["status"] = "failed"
            job["error"] = error
            job["hint"] = hint
            job["steps"].append(error)

    def public(self, job_id: str) -> dict[str, Any] | None:
        """Return the API view of a job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            payload: dict[str, Any] = {
                "status": job["status"],
                "steps": list(job["steps"]),
                "hits": copy.deepcopy(job["hits"]),
            }
            if job["skipped_cameras"]:
                payload["skipped_cameras"] = list(job["skipped_cameras"])
            if job["error"]:
                payload["error"] = job["error"]
                payload["hint"] = job["hint"]
            return payload

    def hit(self, segment_id: str) -> dict[str, Any] | None:
        """Return one stored hit."""
        with self._lock:
            found = self._hits.get(segment_id)
            return copy.deepcopy(found) if found else None

    def remember(self, hits: list[dict[str, Any]]) -> None:
        """Index hits loaded from a snapshot."""
        with self._lock:
            for hit in hits:
                segment_id = hit.get("segment_id")
                if segment_id:
                    self._hits[str(segment_id)] = hit


store = JobStore()


def start_investigation(
    query: str | None,
    cameras: list[str] | None,
    max_hits: int | None,
) -> str:
    """Start the pipeline on a background thread and return the job id."""
    job_id = store.create()
    thread = threading.Thread(
        target=_run,
        args=(job_id, query, cameras, max_hits),
        name=f"investigate-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_id


def remember_snapshot() -> None:
    """Load the last successful run into the hit index."""
    payload = load_snapshot()
    if not payload:
        return
    hits = payload.get("hits")
    if isinstance(hits, list):
        store.remember([hit for hit in hits if isinstance(hit, dict)])


def build_queries(custom: str | None) -> list[str]:
    """Return the default queries, plus a custom query when it is new."""
    queries = list(DEFAULT_QUERIES)
    text = (custom or "").strip()
    if text and text not in queries:
        queries.append(text)
    return queries


def _run(
    job_id: str,
    query: str | None,
    cameras: list[str] | None,
    max_hits: int | None,
) -> None:
    try:
        store.mark_running(job_id)
        selected = cameras if cameras else get_client().list_cameras()
        if not selected:
            store.fail(
                job_id,
                "No indexed cameras were returned.",
                "Check login, then the dashboard, then valid metadata values.",
            )
            return
        queries = build_queries(query)
        candidates = find_candidates(
            queries,
            selected,
            8,
            on_step=lambda message: store.add_step(job_id, message),
        )
        store.add_step(job_id, f"Ranking {len(candidates)} candidates")
        ranked = rank_candidates(candidates)
        if max_hits is not None:
            ranked = ranked[:max_hits]
        store.add_step(job_id, "Assembling evidence")
        evidence = assemble_evidence(ranked)
        hits = [hit.to_dict() for hit in evidence]
        save_snapshot(hits, queries, list(selected))
        store.succeed(job_id, hits)
    except ConfigError as exc:
        store.fail(job_id, f"Missing {exc.name}.", f"Set {exc.name} and run Investigate again.")
    except VssError as exc:
        store.fail(job_id, str(exc), exc.hint)
    except Exception:
        logger.exception("investigate job failed")
        store.fail(job_id, "The investigation failed.", "Run Investigate again.")
