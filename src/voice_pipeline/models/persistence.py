from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import Field, model_validator
from pydantic import JsonValue as PydanticJsonValue

from voice_pipeline.models.model_profiles import ModelProfileSnapshot
from voice_pipeline.models.schemas import (
    EmotionVector,
    EngineFingerprint,
    LanguageCode,
    NonBlankText,
    StrictModel,
)

JobKind = Literal["reference", "gsv", "segment"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
ArtifactType = Literal["reference", "gsv"]
ArtifactState = Literal["ready", "deleting", "deleted", "missing", "corrupt"]
ActivationOutcome = Literal["not_applicable", "activated", "history_only", "cancelled"]
JsonValue: TypeAlias = PydanticJsonValue


class OutputAudioSpec(StrictModel):
    format: Literal["wav"] = "wav"
    sample_rate: int | None = Field(default=None, ge=8_000, le=192_000)
    channels: Literal[1] = 1
    sample_width_bits: Literal[16] = 16


class GsvModelSnapshot(StrictModel):
    profile: ModelProfileSnapshot
    engine_fingerprint: EngineFingerprint


class SegmentJobSnapshot(StrictModel):
    task_id: UUID
    segment_id: UUID
    ref_draft_revision: int = Field(ge=0)
    gsv_draft_revision: int = Field(ge=0)
    selection_revision: int = Field(ge=0)
    active_ref_version_id: UUID | None = None
    active_gsv_version_id: UUID | None = None
    activate_on_success: bool


class PersistentJobRecord(StrictModel):
    job_id: UUID
    request_id: UUID
    kind: JobKind
    status: JobStatus
    stage: str
    request_snapshot: dict[str, JsonValue]
    request_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_fingerprint: dict[str, JsonValue]
    model_profile_snapshot: GsvModelSnapshot | None = None
    output_spec: OutputAudioSpec | None = None
    task_snapshot: SegmentJobSnapshot | None = None
    retry_of_job_id: UUID | None = None
    attempt: int = Field(ge=1)
    cancel_requested_at_utc: datetime | None = None
    result: dict[str, JsonValue] | None = None
    error: dict[str, JsonValue] | None = None
    activation_outcome: ActivationOutcome = "not_applicable"
    created_at_utc: datetime
    started_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None

    @property
    def created_at(self) -> datetime:
        """Batch 1 compatible status field alias."""
        return self.created_at_utc

    @property
    def started_at(self) -> datetime | None:
        """Batch 1 compatible status field alias."""
        return self.started_at_utc

    @property
    def finished_at(self) -> datetime | None:
        """Batch 1 compatible status field alias."""
        return self.finished_at_utc


class JobSuccessCommit(StrictModel):
    result: dict[str, JsonValue]
    activation_outcome: ActivationOutcome = "not_applicable"
    artifact_version_ids: tuple[UUID, ...] = ()
    reference_cache_hit: bool = False
    gsv_cache_hit: bool = False
    terminal_committed: bool = False


class VersionCommitResult(StrictModel):
    version: ArtifactVersionRecord
    activation_outcome: ActivationOutcome
    terminal_status: Literal["succeeded", "cancelled"]


class CancelDecision(StrictModel):
    action: Literal[
        "queued_cancelled",
        "running_cancel_requested",
        "already_cancelled",
        "terminal_conflict",
    ]
    record: PersistentJobRecord


class RecoverySummary(StrictModel):
    interrupted_job_ids: tuple[UUID, ...]
    queued_job_ids: tuple[UUID, ...]


class DispatcherStats(StrictModel):
    state: Literal["stopped", "running", "stopping"]
    queued_count: int = Field(ge=0)
    active_job_id: UUID | None
    recovered_interrupted_count: int = Field(ge=0)


class RetryJobRequest(StrictModel):
    mode: Literal["frozen_snapshot"] = "frozen_snapshot"


class SegmentReferenceJobRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    activate_on_success: bool = True


class SegmentGsvJobRequest(StrictModel):
    request_id: UUID
    activate_on_success: bool = True
    model_profile_id: UUID | None = None


class SegmentBothRegenerationRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    model_profile_id: UUID | None = None


class ActivateVersionRequest(StrictModel):
    expected_selection_revision: int = Field(ge=0)


class RestoreVersionInputsRequest(StrictModel):
    expected_ref_draft_revision: int = Field(ge=0)
    expected_gsv_draft_revision: int = Field(ge=0)


class SegmentInputsPatch(StrictModel):
    expected_ref_draft_revision: int = Field(ge=0)
    expected_gsv_draft_revision: int = Field(ge=0)
    ref_text_cn: NonBlankText | None = None
    current_emotion_vector: EmotionVector | None = None
    synthesis_text: NonBlankText | None = None
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    pause_after_ms: int | None = Field(default=None, ge=0, le=30_000)
    seed: int | None = None

    @model_validator(mode="after")
    def require_change(self) -> SegmentInputsPatch:
        values = (
            self.ref_text_cn,
            self.current_emotion_vector,
            self.synthesis_text,
            self.speed_factor,
            self.pause_after_ms,
            self.seed,
        )
        if all(value is None for value in values):
            raise ValueError("at least one segment input must change")
        return self


class ArtifactVersionRecord(StrictModel):
    version_id: UUID
    segment_id: UUID | None
    artifact_type: ArtifactType
    source_job_id: UUID
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_relative_path: str
    ref_version_id: UUID | None = None
    ref_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_snapshot: dict[str, JsonValue]
    model_fingerprint: dict[str, JsonValue]
    model_profile_snapshot: GsvModelSnapshot | None = None
    quality_result: dict[str, JsonValue]
    state: ArtifactState = "ready"
    created_at_utc: datetime | None = None

    @model_validator(mode="after")
    def require_reference_for_gsv(self) -> ArtifactVersionRecord:
        if self.artifact_type == "gsv" and (
            self.ref_version_id is None or self.ref_content_sha256 is None
        ):
            raise ValueError("gsv artifact requires reference version and hash")
        return self


class ArtifactVersionView(ArtifactVersionRecord):
    blob_relative_path: Path
    state: ArtifactState
    diagnostic: dict[str, JsonValue]


class DubbingTaskRecord(StrictModel):
    task_id: UUID
    title: NonBlankText
    source_text: NonBlankText
    target_language: LanguageCode
    output_spec: OutputAudioSpec
    revision: int = Field(ge=0)
    created_at_utc: datetime
    updated_at_utc: datetime


class CreateDubbingTaskRequest(StrictModel):
    title: NonBlankText
    source_text: NonBlankText
    target_language: LanguageCode
    output_spec: OutputAudioSpec


class CreateSegmentRequest(StrictModel):
    ordinal: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: NonBlankText
    synthesis_text: NonBlankText
    llm_emotion_vector: EmotionVector
    ref_text_cn: NonBlankText
    speed_factor: float = Field(ge=0.5, le=2.0)
    pause_after_ms: int = Field(ge=0, le=30_000)
    seed: int

    @model_validator(mode="after")
    def source_range_must_be_forward(self) -> CreateSegmentRequest:
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class SegmentRecord(StrictModel):
    segment_id: UUID
    task_id: UUID
    ordinal: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: NonBlankText
    synthesis_text: NonBlankText
    target_language: LanguageCode
    llm_emotion_vector: EmotionVector
    current_emotion_vector: EmotionVector
    ref_text_cn: NonBlankText
    speed_factor: float = Field(ge=0.5, le=2.0)
    pause_after_ms: int = Field(ge=0, le=30_000)
    seed: int
    ref_draft_revision: int = Field(ge=0)
    gsv_draft_revision: int = Field(ge=0)
    selection_revision: int = Field(ge=0)
    active_ref_version_id: UUID | None = None
    active_gsv_version_id: UUID | None = None
    revision: int = Field(ge=0)
    created_at_utc: datetime
    updated_at_utc: datetime
