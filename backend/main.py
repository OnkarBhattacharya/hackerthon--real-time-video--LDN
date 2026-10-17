"""FastAPI app for investigations, reports, and the static page."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import AUDIO_AVAILABLE, CROP_AVAILABLE, settings
from backend.evidence import load_snapshot
from backend.jobs import remember_snapshot, start_investigation, store
from backend.models import evidence_from_dict
from backend.reports import (
    TicketWebhookError,
    incident_report,
    ticket_payload,
    translate_report,
)
from backend.vss import VssError, get_client

logger = logging.getLogger(__name__)

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
SAMPLE_CLIP = Path(__file__).resolve().parent / "assets" / "sample.mp4"


class ApiError(Exception):
    """JSON error returned to the browser."""

    def __init__(self, status_code: int, error: str, hint: str) -> None:
        self.status_code = status_code
        self.error = error
        self.hint = hint
        super().__init__(error)


class InvestigateBody(BaseModel):
    """Body for POST /api/investigate."""

    query: str | None = None
    cameras: list[str] | None = None
    max_hits: int | None = Field(default=None, ge=1, le=40)


def create_app() -> FastAPI:
    """Build the app. Routes stay unprefixed so an ingress can rewrite /app to /."""
    app = FastAPI(title="Near-miss investigation", version="0.2.0")
    app.include_router(_router(), prefix="/api")

    @app.exception_handler(ApiError)
    def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error, "hint": exc.hint},
        )

    @app.exception_handler(RequestValidationError)
    def _validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": "The request body is not valid.",
                "hint": "Send JSON with optional query, cameras, and max_hits.",
            },
        )

    @app.exception_handler(Exception)
    def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, ApiError):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.error, "hint": exc.hint},
            )
        logger.exception("request failed")
        return JSONResponse(
            status_code=500,
            content={
                "error": "The investigation service failed.",
                "hint": "Check the server log and try again.",
            },
        )

    if FRONTEND.is_dir():
        app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
    remember_snapshot()
    return app


def _router():
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        """Return service status and which optional features are available."""
        ready = settings.use_fixture() or settings.vss_configured()
        indexed = None
        if ready:
            try:
                indexed = get_client().indexed_segment_count()
            except Exception:
                logger.info("health indexed count unavailable")
                indexed = None
        snapshot = load_snapshot()
        body: dict = {
            "status": "ok" if ready else "missing_config",
            "features": {
                "crop": CROP_AVAILABLE,
                "audio": AUDIO_AVAILABLE,
                "webhook": settings.ticket_webhook_url() is not None,
                "fixture": settings.use_fixture(),
            },
            "indexed_segments": indexed,
            "generated_at": snapshot.get("generated_at") if snapshot else None,
        }
        if not ready:
            body["missing"] = settings.missing_vss()
        return body

    @router.post("/investigate")
    def investigate(body: InvestigateBody) -> dict[str, str]:
        """Start a background investigation."""
        if not settings.use_fixture() and not settings.vss_configured():
            missing = ", ".join(settings.missing_vss())
            raise ApiError(500, f"Missing {missing}.", "Set the VSS login variables and try again.")
        if body.cameras is not None and len(body.cameras) == 0:
            raise ApiError(
                400,
                "No cameras were requested.",
                "Omit cameras to search every indexed camera.",
            )
        job_id = start_investigation(body.query, body.cameras, body.max_hits)
        return {"job_id": job_id}

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> dict:
        """Return job status, progress steps, and hits when the job has finished."""
        payload = store.public(job_id)
        if payload is None:
            raise ApiError(
                404,
                "Unknown job.",
                "Start an investigation and use the job_id from that response.",
            )
        return payload

    @router.get("/hits/{segment_id}")
    def hit(segment_id: str) -> dict:
        """Return one evidence hit."""
        return _require_hit(segment_id)

    @router.post("/hits/{segment_id}/report")
    def report(segment_id: str, lang: str = "en") -> dict[str, str]:
        """Return the English or German report."""
        if lang not in {"en", "de"}:
            raise ApiError(400, "Unsupported report language.", "Use lang=en or lang=de.")
        evidence = evidence_from_dict(_require_hit(segment_id))
        english = incident_report(evidence)
        if lang == "en":
            return english.to_dict()
        return translate_report(english, hit=evidence).to_dict()

    @router.post("/hits/{segment_id}/ticket")
    def ticket(segment_id: str) -> dict:
        """Return the ticket payload, posting it when a webhook is configured."""
        evidence = evidence_from_dict(_require_hit(segment_id))
        try:
            return ticket_payload(evidence, incident_report(evidence))
        except TicketWebhookError as exc:
            raise ApiError(
                502,
                str(exc),
                "The ticket was not delivered. Check the webhook service and try again.",
            ) from exc

    @router.get("/fixture/sample.mp4")
    def fixture_clip() -> FileResponse:
        """Serve the local sample clip used by fixture mode."""
        if not settings.use_fixture():
            raise ApiError(404, "Fixture clips are disabled.", "Set VSS_USE_FIXTURE=1 to use sample clips.")
        if not SAMPLE_CLIP.is_file():
            raise ApiError(404, "The sample clip is missing.", "Generate backend/assets/sample.mp4 and reload.")
        return FileResponse(SAMPLE_CLIP, media_type="video/mp4")

    @router.get("/stream")
    def stream(source: str, request: Request) -> StreamingResponse:
        """Proxy a VSS clip. The browser uses a relative URL with no bearer token."""
        if not source.startswith("s3://"):
            raise ApiError(
                400,
                "The clip source is not playable.",
                "Use a clip URL returned by an investigation.",
            )
        try:
            upstream = get_client().open_stream(source, request.headers.get("range"))
        except VssError as exc:
            raise ApiError(502, str(exc), exc.hint) from exc
        if upstream.status_code >= 400:
            upstream.close()
            raise ApiError(502, "The clip could not be opened.", "Run the investigation again.")
        headers = {}
        for name in ("content-length", "content-range", "accept-ranges"):
            value = upstream.headers.get(name)
            if value:
                headers[name] = value

        def generate():
            try:
                for chunk in upstream.iter_bytes(64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(
            generate(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "video/mp4"),
            headers=headers,
        )

    @router.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def unknown(full_path: str) -> None:
        """JSON 404 for API paths that are not part of this app."""
        raise ApiError(
            404,
            "That API path does not exist.",
            "Use /api/health, /api/investigate, /api/jobs/{job_id}, or /api/hits/{segment_id}.",
        )

    return router


def _require_hit(segment_id: str) -> dict:
    found = store.hit(segment_id)
    if found is None:
        raise ApiError(
            404,
            "Unknown segment.",
            "Run an investigation and use a segment id from the results.",
        )
    return found


app = create_app()


def main() -> None:
    """Run the HTTP server."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.port())


if __name__ == "__main__":
    main()
