from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent import AgentService
from app.config import settings, video_dir
from app.ffmpeg import FfmpegService
from app.ingest import IngestService
from app.localize import LocalizeService
from app.models import ModelClient
from app.schemas import (
    AskRequest,
    AskResponse,
    IngestRequest,
    IngestResponse,
    JobState,
    LocalizeRequest,
    LocalizeResponse,
    StatusResponse,
)
from app.store import MomentStore
from app.yolo import YoloService

models = ModelClient()
ffmpeg = FfmpegService()
yolo = YoloService()
store = MomentStore(models)
ingest_service = IngestService(ffmpeg, models, yolo, store)
agent_service = AgentService(models, store)
localize_service = LocalizeService(ffmpeg, models, store)

app = FastAPI(
    title="Hackerthon Real-Time Video LDN",
    version="0.1.0",
    description="Ingest, index, agent Q&A, and localization pipeline",
)

settings.data_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/media",
    StaticFiles(directory=str(settings.data_dir)),
    name="media",
)


@app.post("/api/ingest", response_model=IngestResponse)
async def api_ingest(
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    vid: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    body: IngestRequest | None = None,
):
    req_vid = vid or (body.vid if body else None)
    req_url = source_url or (body.source_url if body else None)

    job_vid = ingest_service.create_job(req_vid)

    def _ingest_from_url() -> None:
        try:
            path = ingest_service.fetch_source_url(job_vid, req_url)  # type: ignore[arg-type]
            ingest_service.run_ingest(job_vid, path)
        except Exception as exc:
            ingest_service._set_job(
                job_vid,
                state=JobState.failed,
                message="Ingest failed",
                error=str(exc),
            )

    if file and file.filename:
        suffix = Path(file.filename).suffix or ".mp4"
        tmp_dir = Path(tempfile.mkdtemp())
        tmp_path = tmp_dir / f"upload{suffix}"
        content = await file.read()
        tmp_path.write_bytes(content)
        dest = ingest_service.save_upload(job_vid, tmp_path, file.filename)
        background_tasks.add_task(ingest_service.run_ingest, job_vid, dest)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    elif req_url:
        background_tasks.add_task(_ingest_from_url)
    else:
        raise HTTPException(status_code=400, detail="Provide file upload or source_url")

    return IngestResponse(
        vid=job_vid,
        state=JobState.running if file or req_url else JobState.pending,
        message="Ingest started",
    )


@app.get("/api/status/{vid}", response_model=StatusResponse)
async def api_status(vid: str):
    raw = ingest_service.get_status(vid)
    return StatusResponse(**raw)


@app.post("/api/ask", response_model=AskResponse)
async def api_ask(body: AskRequest):
    answer, trace = agent_service.ask(
        body.vid, body.question, max_tool_rounds=body.max_tool_rounds
    )
    return AskResponse(vid=body.vid, answer=answer, tool_trace=trace)


@app.post("/api/localize", response_model=LocalizeResponse)
async def api_localize(body: LocalizeRequest, background_tasks: BackgroundTasks):
    moment = store.get_moment(body.vid, body.moment_id)
    if not moment:
        raise HTTPException(status_code=404, detail="Moment not found")

    background_tasks.add_task(
        localize_service.run_localize,
        body.vid,
        body.moment_id,
        body.target_lang,
        body.include_subtitles,
        body.include_dub,
    )
    return LocalizeResponse(
        vid=body.vid,
        moment_id=body.moment_id,
        state=JobState.running,
        message="Localization started",
    )


@app.get("/api/localize/{vid}/{moment_id}", response_model=LocalizeResponse)
async def api_localize_status(vid: str, moment_id: str):
    job = localize_service.get_job(vid, moment_id)
    if not job:
        raise HTTPException(status_code=404, detail="No localization job")
    return LocalizeResponse(
        vid=vid,
        moment_id=moment_id,
        state=job["state"],
        clip_url=job.get("clip_url"),
        vtt_url=job.get("vtt_url"),
        dub_url=job.get("dub_url"),
        compliance_flags=job.get("compliance_flags") or [],
        message=job.get("message") or "",
    )


@app.get("/api/health")
async def health():
    root = video_dir("_health")
    return {
        "ok": True,
        "yolo": yolo.available,
        "llm": models.has_llm,
        "embeddings": models.has_embeddings,
        "data_dir": str(settings.data_dir.resolve()),
    }


def _mount_frontend() -> None:
    dist = settings.web_dist
    if not dist.exists():
        return
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index = dist / "index.html"

    @app.get("/", include_in_schema=False)
    async def spa_index():
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="Build web UI: cd web && npm run build")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("media/"):
            raise HTTPException(status_code=404)
        if index.exists():
            return FileResponse(index)
        raise HTTPException(status_code=404)


_mount_frontend()
