from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic import JsonValue as PydanticJsonValue

from voice_pipeline.models.schemas import (
    ChineseReferenceText,
    EmotionVector,
    LanguageCode,
    NonBlankText,
    PreservedNonBlankText,
    StrictModel,
)

SourceLanguage = Literal["auto", "zh", "ja", "en", "ko", "yue"]
DirectorProjectStatus = Literal[
    "draft",
    "analyzing",
    "role_review",
    "translating",
    "translation_review",
    "voice_mapping",
    "ready",
    "generating",
    "generation_incomplete",
    "succeeded",
]
DirectorRoleKind = Literal["narrator", "character", "unknown"]
UtteranceKind = Literal["dialogue", "narration", "stage_direction"]
DirectorGenerationStatus = Literal[
    "queued", "running", "generation_incomplete", "succeeded", "interrupted"
]
DirectorGenerationItemStatus = Literal[
    "queued", "reference_running", "reference_ready", "gsv_running", "ready", "failed"
]


class CreateDirectorProjectRequest(StrictModel):
    title: NonBlankText
    source_text: PreservedNonBlankText
    source_language: SourceLanguage = "auto"
    target_language: LanguageCode
    narration_enabled: bool = True


class DirectorProjectRecord(StrictModel):
    project_id: UUID
    title: str
    source_text: str
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_language: SourceLanguage
    target_language: LanguageCode
    narration_enabled: bool
    status: DirectorProjectStatus
    revision: int = Field(ge=0)
    analysis_revision: int = Field(ge=0)
    role_revision: int = Field(ge=0)
    translation_revision: int = Field(ge=0)
    mapping_revision: int = Field(ge=0)
    generation_revision: int = Field(ge=0)
    current_generation_id: UUID | None = None
    final_relative_path: str | None = None
    timeline: dict[str, PydanticJsonValue] | None = None
    last_error: dict[str, PydanticJsonValue] | None = None
    created_at_utc: datetime
    updated_at_utc: datetime
    deleted_at_utc: datetime | None = None


class CreateDirectorRole(StrictModel):
    canonical_name: NonBlankText
    kind: DirectorRoleKind
    aliases: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class DirectorRoleRecord(StrictModel):
    role_id: UUID
    project_id: UUID
    canonical_name: str
    kind: DirectorRoleKind
    aliases: tuple[str, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    preset_id: UUID | None = None
    revision: int = Field(ge=0)


class CreateDirectorUtterance(StrictModel):
    ordinal: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: PreservedNonBlankText
    kind: UtteranceKind
    speak_enabled: bool
    role_id: UUID | None = None
    role_name: str | None = None
    role_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    role_confirmed: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> CreateDirectorUtterance:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class DirectorUtteranceRecord(StrictModel):
    utterance_id: UUID
    project_id: UUID
    ordinal: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: PreservedNonBlankText
    kind: UtteranceKind
    speak_enabled: bool
    role_id: UUID | None = None
    role_confidence: float = Field(ge=0.0, le=1.0)
    role_confirmed: bool
    synthesis_text: str | None = None
    ref_text_cn: str | None = None
    emotion_vector: EmotionVector | None = None
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    pause_after_ms: int = Field(default=0, ge=0, le=30_000)
    seed: int = 1234
    revision: int = Field(ge=0)
    task_id: UUID | None = None
    segment_id: UUID | None = None
    reference_version_id: UUID | None = None
    gsv_version_id: UUID | None = None

    @model_validator(mode="after")
    def validate_range(self) -> DirectorUtteranceRecord:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class DirectorUtterancePatch(StrictModel):
    expected_revision: int = Field(ge=0)
    role_id: UUID | None = None
    speak_enabled: bool | None = None
    role_confirmed: bool | None = None
    synthesis_text: NonBlankText | None = None
    ref_text_cn: ChineseReferenceText | None = None
    emotion_vector: EmotionVector | None = None
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    pause_after_ms: int | None = Field(default=None, ge=0, le=30_000)


class BulkDirectorUtterancePatch(StrictModel):
    utterance_ids: tuple[UUID, ...] = Field(min_length=1)
    expected_revisions: dict[UUID, int]
    role_id: UUID
    role_confirmed: bool = True


class SplitDirectorUtteranceRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    split_at: int = Field(gt=0)


class MergeDirectorUtterancesRequest(StrictModel):
    left_utterance_id: UUID
    right_utterance_id: UUID
    expected_left_revision: int = Field(ge=0)
    expected_right_revision: int = Field(ge=0)


class ExpectedProjectRevision(StrictModel):
    expected_revision: int = Field(ge=0)


class BindRolePresetRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    preset_id: UUID


class DirectorRolePatch(StrictModel):
    expected_revision: int = Field(ge=0)
    canonical_name: NonBlankText | None = None
    aliases: tuple[str, ...] | None = None


class MergeDirectorRolesRequest(StrictModel):
    project_id: UUID
    expected_project_revision: int = Field(ge=0)
    source_role_ids: tuple[UUID, ...] = Field(min_length=1)
    target_role_id: UUID


class SplitDirectorRoleRequest(StrictModel):
    project_id: UUID
    expected_project_revision: int = Field(ge=0)
    source_role_id: UUID
    utterance_ids: tuple[UUID, ...] = Field(min_length=1)
    canonical_name: NonBlankText


class NarrationSettingRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    enabled: bool


class CreateRolePresetRequest(StrictModel):
    name: NonBlankText
    base_voice_path: Path
    model_profile_id: UUID
    default_speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @field_validator("base_voice_path")
    @classmethod
    def require_wav(cls, value: Path) -> Path:
        if value.suffix.casefold() != ".wav":
            raise ValueError("base voice must be a WAV file")
        return value


class UpdateRolePresetRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    name: NonBlankText | None = None
    model_profile_id: UUID | None = None
    default_speed: float | None = Field(default=None, ge=0.5, le=2.0)


class RolePresetRecord(StrictModel):
    preset_id: UUID
    name: str
    base_voice_relative_path: str
    base_voice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    sample_rate: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=2)
    model_profile_id: UUID
    default_speed: float = Field(ge=0.5, le=2.0)
    status: Literal["ready", "missing", "corrupt", "archived"]
    revision: int = Field(ge=0)
    created_at_utc: datetime
    updated_at_utc: datetime


class DirectorGenerationRecord(StrictModel):
    generation_id: UUID
    project_id: UUID
    project_revision: int = Field(ge=0)
    status: DirectorGenerationStatus
    snapshot: dict[str, PydanticJsonValue]
    final_relative_path: str | None = None
    timeline: dict[str, PydanticJsonValue] | None = None
    error: dict[str, PydanticJsonValue] | None = None
    created_at_utc: datetime
    started_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None


class DirectorGenerationItemRecord(StrictModel):
    generation_id: UUID
    utterance_id: UUID
    ordinal: int = Field(ge=0)
    model_profile_id: UUID
    status: DirectorGenerationItemStatus
    reference_job_id: UUID | None = None
    gsv_job_id: UUID | None = None
    error: dict[str, PydanticJsonValue] | None = None
