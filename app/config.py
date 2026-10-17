from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    web_dist: Path = Path("web/dist")

    anthropic_api_key: str | None = None  # ANTHROPIC_API_KEY
    openai_api_key: str | None = None  # OPENAI_API_KEY

    vision_model: str = "claude-sonnet-4-20250514"
    llm_model: str = "claude-sonnet-4-20250514"
    embedding_model: str = "text-embedding-3-small"

    whisper_model_size: str = "base"
    tts_voice_de: str = "de-DE-KatjaNeural"

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    chunk_duration_sec: float = 10.0
    frames_per_chunk: int = 4
    yolo_model: str = "yolov8n.pt"
    yolo_confidence: float = 0.35

    enable_yolo: bool = True
    enable_whisper: bool = True
    enable_tts: bool = False


settings = Settings()


def video_dir(vid: str) -> Path:
    return settings.data_dir / vid


def ensure_video_layout(vid: str) -> Path:
    root = video_dir(vid)
    for sub in ("chunks", "frames", "dubs", "clips"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
