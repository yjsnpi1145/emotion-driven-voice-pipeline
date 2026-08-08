from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from typing import Protocol

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import EmotionVector
from voice_pipeline.modules.llm.models import (
    CorrectionDirection,
    DirectedSegment,
    DirectorPlan,
    MaterializedDirectedSegment,
)


class ReferenceDurationProbe(Protocol):
    async def generate_and_measure(self, text: str, vector: EmotionVector, seed: int) -> float: ...


class ReferenceTextCorrector(Protocol):
    def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
    ) -> Awaitable[str]: ...


class ResolvedDirectedSegment(DirectedSegment):
    reference_corrections: int


class ReferenceTextDirector:
    def __init__(self, corrector: ReferenceTextCorrector) -> None:
        self._corrector = corrector

    async def resolve_reference_text(
        self,
        segment: DirectedSegment,
        probe: ReferenceDurationProbe,
        *,
        max_corrections: int,
    ) -> ResolvedDirectedSegment:
        current = segment.ref_text_cn
        for correction in range(max_corrections + 1):
            duration_seconds = await probe.generate_and_measure(
                current, segment.emotion_vector, segment.seed
            )
            if 3.0 <= duration_seconds <= 9.0:
                return ResolvedDirectedSegment.model_validate(
                    {
                        **segment.model_dump(),
                        "ref_text_cn": current,
                        "reference_corrections": correction,
                    }
                )
            if correction == max_corrections:
                raise PipelineError(
                    ErrorCode.REFERENCE_DURATION_INVALID,
                    "llm",
                    "reference duration is outside 3.0..9.0 after corrections",
                    retryable=False,
                    details={"duration_seconds": duration_seconds, "corrections": correction},
                )
            direction: CorrectionDirection = "shorten" if duration_seconds > 9.0 else "lengthen"
            current = await self._corrector.correct_reference_text(
                current=current,
                direction=direction,
                emotion_description=segment.emotion_description,
            )
        raise AssertionError("reference correction loop must return or raise")


def validate_director_plan(
    source_text: str, plan: DirectorPlan
) -> tuple[MaterializedDirectedSegment, ...]:
    source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if plan.source_text_sha256 != source_sha256:
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "director source_text_sha256 does not match submitted source text",
            retryable=False,
        )
    if not plan.segments:
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "director plan must contain at least one segment",
            retryable=False,
        )

    materialized: list[MaterializedDirectedSegment] = []
    previous_end = 0
    for expected_ordinal, segment in enumerate(plan.segments):
        if segment.ordinal != expected_ordinal:
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "director segment ordinals must start at zero and be contiguous",
                retryable=False,
            )
        if segment.source_start != previous_end or segment.source_end <= segment.source_start:
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "director segments must cover source text without gaps or overlaps",
                retryable=False,
            )
        if segment.source_end > len(source_text):
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "director segment range is outside source text",
                retryable=False,
            )
        source_slice = source_text[segment.source_start : segment.source_end]
        if not source_slice.strip():
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "director segment source range must not contain blank text only",
                retryable=False,
            )
        materialized.append(
            MaterializedDirectedSegment(
                **segment.model_dump(), source_text=source_slice, synthesis_text=source_slice
            )
        )
        previous_end = segment.source_end
    if previous_end != len(source_text):
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "director segments must cover source text without gaps or overlaps",
            retryable=False,
        )
    return tuple(materialized)
