from __future__ import annotations

import hashlib

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.llm.models import DirectorPlan, MaterializedDirectedSegment


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
