from __future__ import annotations

from typing import Literal

from pydantic import Field

from voice_pipeline.models.schemas import (
    ChineseReferenceText,
    EmotionVector,
    NonBlankText,
    StrictModel,
)


class DirectedSegment(StrictModel):
    ordinal: int = Field(ge=0)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    emotion_description: NonBlankText
    emotion_vector: EmotionVector
    synthesis_text: NonBlankText
    ref_text_cn: ChineseReferenceText
    pause_after_ms: int = Field(ge=0, le=30_000)
    speed_factor: float = Field(ge=0.5, le=2.0)
    seed: int = 1234


class DirectorPlan(StrictModel):
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segments: tuple[DirectedSegment, ...]


class MaterializedDirectedSegment(DirectedSegment):
    source_text: NonBlankText


class ReferenceTextCorrection(StrictModel):
    ref_text_cn: ChineseReferenceText


CorrectionDirection = Literal["shorten", "lengthen"]
