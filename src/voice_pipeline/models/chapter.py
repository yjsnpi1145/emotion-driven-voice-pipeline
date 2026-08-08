from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic import JsonValue as PydanticJsonValue

from voice_pipeline.models.persistence import OutputAudioSpec
from voice_pipeline.models.schemas import AudioResult, LanguageCode, NonBlankText, StrictModel

ChapterRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]


class ChapterSynthesisRequest(StrictModel):
    request_id: UUID
    title: NonBlankText
    source_text: NonBlankText
    target_language: LanguageCode
    base_voice_path: Path
    model_profile_id: UUID
    output_spec: OutputAudioSpec = Field(default_factory=OutputAudioSpec)
    seed: int = 1234


class ChapterTimelineSegment(StrictModel):
    ordinal: int = Field(ge=0)
    segment_id: UUID
    gsv_version_id: UUID
    gsv_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    pause_after_ms: int = Field(ge=0, le=30_000)


class ChapterTimeline(StrictModel):
    schema_version: Literal[1] = 1
    segments: tuple[ChapterTimelineSegment, ...]
    duration_seconds: float = Field(gt=0)


class ChapterRunRecord(StrictModel):
    run_id: UUID
    request_id: UUID
    task_id: UUID
    status: ChapterRunStatus
    snapshot: dict[str, PydanticJsonValue]
    director_plan: dict[str, PydanticJsonValue]
    base_voice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_audio: AudioResult | None = None
    final_relative_path: str | None = None
    timeline: ChapterTimeline | None = None
    error: dict[str, PydanticJsonValue] | None = None
    created_at_utc: datetime
    started_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None
