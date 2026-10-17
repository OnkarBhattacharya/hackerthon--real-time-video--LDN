from __future__ import annotations

import threading
from pathlib import Path

from app.config import ensure_video_layout, video_dir
from app.ffmpeg import FfmpegService
from app.models import ModelClient
from app.schemas import JobState
from app.store import MomentStore


def _write_vtt(segments: list[dict], path: Path) -> None:
    def fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{fmt(seg['start'])} --> {fmt(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


class LocalizeService:
    def __init__(
        self,
        ffmpeg: FfmpegService,
        models: ModelClient,
        store: MomentStore,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.models = models
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _key(self, vid: str, moment_id: str) -> str:
        return f"{vid}:{moment_id}"

    def get_job(self, vid: str, moment_id: str) -> dict | None:
        with self._lock:
            return self._jobs.get(self._key(vid, moment_id))

    def run_localize(
        self,
        vid: str,
        moment_id: str,
        target_lang: str = "de-DE",
        include_subtitles: bool = True,
        include_dub: bool = False,
    ) -> None:
        key = self._key(vid, moment_id)
        try:
            with self._lock:
                self._jobs[key] = {
                    "state": JobState.running,
                    "message": "Cutting clip",
                    "clip_url": None,
                    "vtt_url": None,
                    "dub_url": None,
                    "compliance_flags": [],
                }
            moment = self.store.get_moment(vid, moment_id)
            if not moment:
                raise ValueError(f"Unknown moment {moment_id}")

            root = ensure_video_layout(vid)
            sources = list(root.glob("source*"))
            if not sources:
                raise FileNotFoundError("No source video for this id")
            source = sources[0]

            clip_name = f"{moment_id}.mp4"
            clip_path = root / "clips" / clip_name
            self.ffmpeg.cut_clip(
                source,
                float(moment["start_sec"]),
                float(moment["end_sec"]),
                clip_path,
            )
            clip_url = f"/media/{vid}/clips/{clip_name}"

            vtt_url = None
            dub_url = None
            flags: list[str] = []

            if include_subtitles or include_dub:
                wav = root / "dubs" / f"{moment_id}.wav"
                self.ffmpeg.extract_audio(clip_path, wav)
                segments = self.models.transcribe_wav(wav)
                translated, flags = self.models.translate_segments(
                    segments, target_lang
                )
                if include_subtitles:
                    vtt_path = root / "dubs" / f"{moment_id}.{target_lang}.vtt"
                    _write_vtt(translated, vtt_path)
                    vtt_url = f"/media/{vid}/dubs/{vtt_path.name}"

                if include_dub:
                    text = " ".join(s["text"] for s in translated)
                    tts_path = root / "dubs" / f"{moment_id}.{target_lang}.mp3"
                    out = self.models.synthesize_speech_de(text, tts_path)
                    if out:
                        dubbed = root / "dubs" / f"{moment_id}.{target_lang}.dubbed.mp4"
                        self.ffmpeg.mux_audio(clip_path, out, dubbed)
                        dub_url = f"/media/{vid}/dubs/{dubbed.name}"

            with self._lock:
                self._jobs[key] = {
                    "state": JobState.completed,
                    "message": "Localization complete",
                    "clip_url": clip_url,
                    "vtt_url": vtt_url,
                    "dub_url": dub_url,
                    "compliance_flags": flags,
                }
        except Exception as exc:
            with self._lock:
                self._jobs[key] = {
                    "state": JobState.failed,
                    "message": str(exc),
                    "clip_url": None,
                    "vtt_url": None,
                    "dub_url": None,
                    "compliance_flags": [],
                }
