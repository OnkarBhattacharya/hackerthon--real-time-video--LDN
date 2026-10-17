from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.config import settings


class FfmpegError(RuntimeError):
    pass


class FfmpegService:
    def __init__(self) -> None:
        self.ffmpeg = settings.ffmpeg_bin
        self.ffprobe = settings.ffprobe_bin

    def _run(self, args: list[str]) -> None:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise FfmpegError(
                proc.stderr.strip() or proc.stdout.strip() or "ffmpeg failed"
            )

    def probe_duration(self, path: Path) -> float:
        proc = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        return float(data["format"]["duration"])

    def split_chunks(
        self,
        source: Path,
        out_dir: Path,
        chunk_sec: float,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = out_dir / "chunk_%04d.mp4"
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-i",
                str(source),
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(chunk_sec),
                "-reset_timestamps",
                "1",
                str(pattern),
            ]
        )
        return sorted(out_dir.glob("chunk_*.mp4"))

    def extract_frames(
        self,
        chunk: Path,
        out_dir: Path,
        count: int,
    ) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        duration = max(self.probe_duration(chunk), 0.1)
        frames: list[Path] = []
        for i in range(count):
            t = min(duration * (i + 0.5) / count, max(duration - 0.05, 0))
            out = out_dir / f"{chunk.stem}_f{i:02d}.jpg"
            self._run(
                [
                    self.ffmpeg,
                    "-y",
                    "-ss",
                    str(t),
                    "-i",
                    str(chunk),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(out),
                ]
            )
            frames.append(out)
        return frames

    def cut_clip(
        self,
        source: Path,
        start_sec: float,
        end_sec: float,
        out_path: Path,
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(end_sec - start_sec, 0.1)
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-ss",
                str(start_sec),
                "-i",
                str(source),
                "-t",
                str(duration),
                "-c",
                "copy",
                str(out_path),
            ]
        )
        return out_path

    def extract_audio(self, video: Path, out_wav: Path) -> Path:
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-i",
                str(video),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(out_wav),
            ]
        )
        return out_wav

    def mux_audio(
        self,
        video: Path,
        audio: Path,
        out_path: Path,
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-shortest",
                str(out_path),
            ]
        )
        return out_path
