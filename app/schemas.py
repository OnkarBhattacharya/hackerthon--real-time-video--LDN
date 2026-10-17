from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobState(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class IngestRequest(BaseModel):
    vid: str | None = Field(
        default=None, description="Optional id; generated if omitted"
    )
    source_url: str | None = Field(
        default=None, description="Remote URL to fetch (optional)"
    )


class IngestResponse(BaseModel):
    vid: str
    state: JobState
    message: str


class StatusResponse(BaseModel):
    vid: str
    state: JobState
    progress: float = Field(ge=0, le=1)
    message: str
    moment_count: int = 0
    detection_count: int = 0
    index_path: str | None = None
    error: str | None = None


class AskRequest(BaseModel):
    vid: str
    question: str
    max_tool_rounds: int = 6


class AskResponse(BaseModel):
    vid: str
    answer: str
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class LocalizeRequest(BaseModel):
    vid: str
    moment_id: str
    target_lang: str = "de-DE"
    include_subtitles: bool = True
    include_dub: bool = False


class LocalizeResponse(BaseModel):
    vid: str
    moment_id: str
    state: JobState
    clip_url: str | None = None
    vtt_url: str | None = None
    dub_url: str | None = None
    compliance_flags: list[str] = Field(default_factory=list)
    message: str
