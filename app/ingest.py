from __future__ import annotations

import shutil
import threading
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import ensure_video_layout, settings, video_dir
from app.ffmpeg import FfmpegService
from app.models import ModelClient
from app.schemas import JobState
from app.store import MomentStore
from app.yolo import YoloService


class IngestService:
    def __init__(
        self,
        ffmpeg: FfmpegService,
        models: ModelClient,
        yolo: YoloService,
        store: MomentStore,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.models = models
        self.yolo = yolo
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_job(self, vid: str | None = None) -> str:
        vid = vid or uuid4().hex[:12]
        ensure_video_layout(vid)
        with self._lock:
            self._jobs[vid] = {
                "state": JobState.pending,
                "progress": 0.0,
                "message": "Queued",
                "error": None,
            }
        return vid

    def get_status(self, vid: str) -> dict:
        with self._lock:
            job = self._jobs.get(vid)
        index = self.store.load(vid)
        moment_count = len(index.get("moments", []))
        detection_count = len(index.get("detections", []))
        if job:
            return {
                "vid": vid,
                "state": job["state"],
                "progress": job["progress"],
                "message": job["message"],
                "error": job.get("error"),
                "moment_count": moment_count,
                "detection_count": detection_count,
                "index_path": str(video_dir(vid) / "index.json"),
            }
        if moment_count > 0:
            return {
                "vid": vid,
                "state": JobState.completed,
                "progress": 1.0,
                "message": "Indexed",
                "error": None,
                "moment_count": moment_count,
                "detection_count": detection_count,
                "index_path": str(video_dir(vid) / "index.json"),
            }
        return {
            "vid": vid,
            "state": JobState.pending,
            "progress": 0.0,
            "message": "Unknown video id",
            "error": None,
            "moment_count": 0,
            "detection_count": 0,
            "index_path": None,
        }

    def _set_job(
        self,
        vid: str,
        *,
        state: JobState | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.setdefault(
                vid,
                {
                    "state": JobState.pending,
                    "progress": 0.0,
                    "message": "",
                    "error": None,
                },
            )
            if state is not None:
                job["state"] = state
            if progress is not None:
                job["progress"] = progress
            if message is not None:
                job["message"] = message
            if error is not None:
                job["error"] = error

    def save_upload(self, vid: str, upload_path: Path, filename: str) -> Path:
        root = ensure_video_layout(vid)
        dest = root / f"source{Path(filename).suffix or '.mp4'}"
        shutil.copy2(upload_path, dest)
        return dest

    def fetch_source_url(self, vid: str, url: str) -> Path:
        root = ensure_video_layout(vid)
        dest = root / "source_remote.mp4"
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return dest

    def run_ingest(self, vid: str, source: Path) -> None:
        try:
            self._set_job(
                vid,
                state=JobState.running,
                progress=0.05,
                message="Splitting video into chunks",
            )
            chunks = self.ffmpeg.split_chunks(
                source,
                video_dir(vid) / "chunks",
                settings.chunk_duration_sec,
            )
            total = max(len(chunks), 1)
            for i, chunk in enumerate(chunks):
                chunk_id = chunk.stem
                start_sec = i * settings.chunk_duration_sec
                end_sec = start_sec + self.ffmpeg.probe_duration(chunk)
                frames = self.ffmpeg.extract_frames(
                    chunk,
                    video_dir(vid) / "frames" / chunk_id,
                    settings.frames_per_chunk,
                )
                primary = frames[0]
                rel_frame = str(primary.relative_to(video_dir(vid)))
                caption = self.models.caption_image(primary)
                moment = self.store.add_moment(
                    vid,
                    chunk_id=chunk_id,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    caption=caption,
                    frame_path=rel_frame,
                )
                for frame in frames:
                    dets = self.yolo.detect_frame(frame)
                    self.store.add_detections(
                        vid,
                        moment_id=moment["moment_id"],
                        frame_path=str(frame.relative_to(video_dir(vid))),
                        items=dets,
                    )
                self._set_job(
                    vid,
                    progress=0.1 + 0.85 * ((i + 1) / total),
                    message=f"Processed chunk {i + 1}/{total}",
                )

            index = self.store.load(vid)
            index["meta"] = {
                "source": str(source.name),
                "chunk_count": len(chunks),
                "duration_sec": len(chunks) * settings.chunk_duration_sec,
            }
            self.store.save(vid, index)
            self._set_job(
                vid,
                state=JobState.completed,
                progress=1.0,
                message="Ingest complete",
            )
        except Exception as exc:
            self._set_job(
                vid,
                state=JobState.failed,
                message="Ingest failed",
                error=str(exc),
            )
