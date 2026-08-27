from __future__ import annotations

import hashlib

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    ChunkAnalysisResult,
    ScriptAnalysisUnit,
    ScriptChunk,
    UnitAnalysisResult,
)

_SAFE_BOUNDARIES = frozenset("。！？.!?；;\n")
_DEFAULT_UNIT_CHARS = 160


def split_script(source_text: str, max_chars: int = 2400) -> tuple[ScriptChunk, ...]:
    """Split without normalization while preferring paragraph and sentence boundaries."""
    if not source_text.strip():
        raise ValueError("source_text must not be blank")
    if max_chars < 8:
        raise ValueError("max_chars must be at least 8")
    chunks: list[ScriptChunk] = []
    start = 0
    while start < len(source_text):
        limit = min(start + max_chars, len(source_text))
        end = limit
        if limit < len(source_text):
            minimum = start + max(1, max_chars // 2)
            for index in range(limit, minimum - 1, -1):
                if source_text[index - 1] in _SAFE_BOUNDARIES:
                    end = index
                    break
        text = source_text[start:end]
        digest = hashlib.sha256(
            f"{start}:{end}:".encode("ascii") + text.encode("utf-8")
        ).hexdigest()
        chunks.append(
            ScriptChunk(
                chunk_id=digest,
                source_start=start,
                source_end=end,
                source_text=text,
            )
        )
        start = end
    return tuple(chunks)


def build_analysis_units(
    chunk: ScriptChunk, max_unit_chars: int = _DEFAULT_UNIT_CHARS
) -> tuple[ScriptAnalysisUnit, ...]:
    """Create stable, contiguous source units without normalizing any character."""
    if max_unit_chars < 8:
        raise ValueError("max_unit_chars must be at least 8")
    text = chunk.source_text
    raw: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(text, start=1):
        if character in _SAFE_BOUNDARIES or index - start >= max_unit_chars:
            raw.append((start, index))
            start = index
    if start < len(text):
        raw.append((start, len(text)))
    if not raw:
        raw.append((0, len(text)))

    merged: list[tuple[int, int]] = []
    pending_blank_start: int | None = None
    for start, end in raw:
        if not text[start:end].strip():
            pending_blank_start = start if pending_blank_start is None else pending_blank_start
            continue
        actual_start = pending_blank_start if pending_blank_start is not None else start
        if end - actual_start > max_unit_chars and actual_start < start:
            merged.append((actual_start, start))
            actual_start = start
        merged.append((actual_start, end))
        pending_blank_start = None
    if pending_blank_start is not None:
        if merged and len(text) - merged[-1][0] <= max_unit_chars:
            previous_start, _ = merged[-1]
            merged[-1] = (previous_start, len(text))
        else:
            merged.append((pending_blank_start, len(text)))
    if not merged:
        merged.append((0, len(text)))

    return tuple(
        ScriptAnalysisUnit(
            unit_id=f"{chunk.chunk_id}:u{ordinal:04d}",
            source_start=chunk.source_start + local_start,
            source_end=chunk.source_start + local_end,
            source_text=text[local_start:local_end],
        )
        for ordinal, (local_start, local_end) in enumerate(merged)
    )


def materialize_unit_analysis(
    chunk: ScriptChunk,
    units: tuple[ScriptAnalysisUnit, ...],
    result: UnitAnalysisResult,
) -> ChunkAnalysisResult:
    _validate_analysis_units(chunk, units)
    expected_ids = tuple(unit.unit_id for unit in units)
    actual_ids = tuple(item.unit_id for item in result.units)
    if actual_ids != expected_ids:
        raise _invalid("analysis unit IDs must match the supplied units exactly and in order")
    materialized = ChunkAnalysisResult(
        utterances=tuple(
            AnalyzedUtterance(
                source_start=unit.source_start,
                source_end=unit.source_end,
                source_text=unit.source_text,
                kind=annotation.kind,
                temporary_role_name=annotation.temporary_role_name,
                role_aliases=annotation.role_aliases,
                role_confidence=annotation.role_confidence,
                speak_enabled=annotation.speak_enabled,
            )
            for unit, annotation in zip(units, result.units, strict=True)
        )
    )
    validate_chunk_analysis(chunk, materialized)
    return materialized


def _validate_analysis_units(chunk: ScriptChunk, units: tuple[ScriptAnalysisUnit, ...]) -> None:
    if not units:
        raise _invalid("analysis chunk contains no local units")
    cursor = chunk.source_start
    for unit in units:
        if unit.source_start != cursor or unit.source_end <= unit.source_start:
            raise _invalid("analysis units have gaps, overlap, or reverse order")
        local_start = unit.source_start - chunk.source_start
        local_end = unit.source_end - chunk.source_start
        if chunk.source_text[local_start:local_end] != unit.source_text:
            raise _invalid("analysis unit text does not match the source slice")
        cursor = unit.source_end
    if cursor != chunk.source_end:
        raise _invalid("analysis units do not cover the complete chunk")


def validate_chunk_analysis(chunk: ScriptChunk, result: ChunkAnalysisResult) -> None:
    if not result.utterances:
        raise _invalid("analysis chunk contains no utterances")
    cursor = chunk.source_start
    for item in result.utterances:
        if item.source_start != cursor or item.source_end <= item.source_start:
            raise _invalid("analysis ranges have gaps, overlap, or reverse order")
        if item.source_end > chunk.source_end:
            raise _invalid("analysis range exceeds its chunk")
        local_start = item.source_start - chunk.source_start
        local_end = item.source_end - chunk.source_start
        if chunk.source_text[local_start:local_end] != item.source_text:
            raise _invalid("analysis source_text does not match the source slice")
        cursor = item.source_end
    if cursor != chunk.source_end:
        raise _invalid("analysis does not cover the complete chunk")


def _invalid(message: str) -> PipelineError:
    return PipelineError(
        ErrorCode.LLM_INVALID_RESPONSE,
        "llm",
        message,
        retryable=False,
    )
