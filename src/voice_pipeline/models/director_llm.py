from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from voice_pipeline.models.schemas import (
    ChineseReferenceText,
    EmotionVector,
    NonBlankText,
    StrictModel,
)


class ScriptChunk(StrictModel):
    chunk_id: str
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: str

    @model_validator(mode="after")
    def validate_range(self) -> ScriptChunk:
        if self.source_end <= self.source_start:
            raise ValueError("chunk range must move forward")
        return self


class AnalyzedUtterance(StrictModel):
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: str
    kind: Literal["dialogue", "narration", "stage_direction"]
    temporary_role_name: str | None = None
    role_aliases: tuple[str, ...] = ()
    role_confidence: float = Field(ge=0.0, le=1.0)
    speak_enabled: bool


class ChunkAnalysisResult(StrictModel):
    utterances: tuple[AnalyzedUtterance, ...]


class ReconciledRole(StrictModel):
    key: str
    canonical_name: NonBlankText
    kind: Literal["narrator", "character", "unknown"]
    aliases: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)


class CandidateRoleAssignment(StrictModel):
    utterance_index: int = Field(ge=0)
    role_key: str
    confidence: float = Field(ge=0.0, le=1.0)


class CastReconciliationResult(StrictModel):
    roles: tuple[ReconciledRole, ...]
    assignments: tuple[CandidateRoleAssignment, ...]


class TranslationInput(StrictModel):
    utterance_id: UUID
    revision: int = Field(ge=0)
    source_text: str


class TranslationResultItem(StrictModel):
    utterance_id: UUID
    revision: int = Field(ge=0)
    synthesis_text: NonBlankText
    ref_text_cn: ChineseReferenceText
    emotion_vector: EmotionVector
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    pause_after_ms: int = Field(default=0, ge=0, le=30_000)


class ScriptTranslationResult(StrictModel):
    items: tuple[TranslationResultItem, ...]
