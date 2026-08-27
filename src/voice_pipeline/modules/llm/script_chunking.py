from __future__ import annotations

import hashlib
from typing import Literal

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    ChunkAnalysisResult,
    ScriptAnalysisUnit,
    ScriptChunk,
    UnitAnalysis,
    UnitAnalysisResult,
)
from voice_pipeline.modules.text.speakability import is_pause_marker, is_speakable_text

_SAFE_BOUNDARIES = frozenset("。！？.!?；;\n")
_DEFAULT_UNIT_CHARS = 160
_QUOTE_CLOSERS = {"“": "”", "「": "」", "『": "』"}
_AnalysisContext = Literal[
    "general",
    "quoted_dialogue",
    "quote_bridge_narration",
    "pause_marker",
]


def split_script(source_text: str, max_chars: int = 2400) -> tuple[ScriptChunk, ...]:
    """Split without normalization while preferring paragraph and sentence boundaries."""
    if not source_text.strip():
        raise ValueError("source_text must not be blank")
    if max_chars < 8:
        raise ValueError("max_chars must be at least 8")
    chunks: list[ScriptChunk] = []
    quote_spans = _balanced_quote_spans(source_text)
    start = 0
    while start < len(source_text):
        limit = min(start + max_chars, len(source_text))
        end = limit
        if limit < len(source_text):
            minimum = start + max(1, max_chars // 2)
            for index in range(limit, minimum - 1, -1):
                if (
                    source_text[index - 1] in _SAFE_BOUNDARIES
                    and not _boundary_inside_quote(index, quote_spans)
                ):
                    end = index
                    break
            else:
                containing = next(
                    (
                        (quote_start, quote_end)
                        for quote_start, quote_end in quote_spans
                        if quote_start <= start < quote_end
                    ),
                    None,
                )
                if containing is not None:
                    end = containing[1]
                else:
                    crossing = next(
                        (
                            (quote_start, quote_end)
                            for quote_start, quote_end in quote_spans
                            if start < quote_start < limit < quote_end
                        ),
                        None,
                    )
                    if crossing is not None:
                        end = crossing[0]
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
    ranges: list[tuple[int, int, _AnalysisContext]] = []
    for local_start, local_end, context in _analysis_context_ranges(text):
        for start, end in _segment_analysis_range(
            text,
            local_start,
            local_end,
            max_unit_chars=max_unit_chars,
            prefer_safe_boundaries=context != "quoted_dialogue",
        ):
            ranges.append((start, end, context))
    ranges = _merge_non_speakable_ranges(text, ranges)

    return tuple(
        ScriptAnalysisUnit(
            unit_id=f"{chunk.chunk_id}:u{ordinal:04d}",
            source_start=chunk.source_start + local_start,
            source_end=chunk.source_start + local_end,
            source_text=text[local_start:local_end],
            context=context,
        )
        for ordinal, (local_start, local_end, context) in enumerate(ranges)
    )


def _analysis_context_ranges(text: str) -> tuple[tuple[int, int, _AnalysisContext], ...]:
    quoted = _balanced_quote_spans(text)
    if not quoted:
        return ((0, len(text), "general"),)

    ranges: list[tuple[int, int, _AnalysisContext]] = []
    cursor = 0
    for index, (start, end) in enumerate(quoted):
        if cursor < start:
            gap = text[cursor:start]
            is_bridge = (
                index > 0 and bool(gap.strip()) and "\n" not in gap and "\r" not in gap
            )
            ranges.append(
                (cursor, start, "quote_bridge_narration" if is_bridge else "general")
            )
        ranges.append((start, end, "quoted_dialogue"))
        cursor = end
    if cursor < len(text):
        ranges.append((cursor, len(text), "general"))
    return tuple(ranges)


def _balanced_quote_spans(text: str) -> tuple[tuple[int, int], ...]:
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if character == '"':
            if stack and stack[-1][0] == '"':
                _, start = stack.pop()
                if not stack:
                    spans.append((start, index + 1))
            else:
                stack.append(('"', index))
            continue
        closer = _QUOTE_CLOSERS.get(character)
        if closer is not None:
            stack.append((closer, index))
            continue
        if stack and character == stack[-1][0]:
            _, start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
    return tuple(spans)


def _boundary_inside_quote(
    boundary: int,
    quote_spans: tuple[tuple[int, int], ...],
) -> bool:
    return any(start < boundary < end for start, end in quote_spans)


def _merge_non_speakable_ranges(
    text: str,
    ranges: list[tuple[int, int, _AnalysisContext]],
) -> list[tuple[int, int, _AnalysisContext]]:
    merged: list[tuple[int, int, _AnalysisContext]] = []
    pending_start: int | None = None
    for start, end, context in ranges:
        value = text[start:end]
        if is_speakable_text(value):
            actual_start = pending_start if pending_start is not None else start
            merged.append((actual_start, end, context))
            pending_start = None
            continue
        pause_value = value.strip().strip("“”「」『』\"")
        if is_pause_marker(pause_value):
            if pending_start is not None:
                start = pending_start
                pending_start = None
            merged.append((start, end, "pause_marker"))
            continue
        if merged:
            previous_start, _, previous_context = merged[-1]
            merged[-1] = (previous_start, end, previous_context)
        else:
            pending_start = start if pending_start is None else pending_start
    if pending_start is not None:
        if merged:
            previous_start, _, previous_context = merged[-1]
            merged[-1] = (previous_start, len(text), previous_context)
        else:
            merged.append((pending_start, len(text), "pause_marker"))
    return merged


def _segment_analysis_range(
    text: str,
    range_start: int,
    range_end: int,
    *,
    max_unit_chars: int,
    prefer_safe_boundaries: bool,
) -> tuple[tuple[int, int], ...]:
    if range_start >= range_end:
        return ()
    raw: list[tuple[int, int]] = []
    start = range_start
    for index in range(range_start + 1, range_end + 1):
        character = text[index - 1]
        if (
            (prefer_safe_boundaries and character in _SAFE_BOUNDARIES)
            or index - start >= max_unit_chars
        ):
            raw.append((start, index))
            start = index
    if start < range_end:
        raw.append((start, range_end))
    if not raw:
        raw.append((range_start, range_end))

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
        if merged and range_end - merged[-1][0] <= max_unit_chars:
            previous_start, _ = merged[-1]
            merged[-1] = (previous_start, range_end)
        else:
            merged.append((pending_blank_start, range_end))
    if not merged:
        merged.append((range_start, range_end))
    return tuple(merged)


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
    constrained = tuple(
        _constrain_unit_annotation(unit, annotation)
        for unit, annotation in zip(units, result.units, strict=True)
    )
    materialized = ChunkAnalysisResult(
        utterances=tuple(
            AnalyzedUtterance(
                source_start=unit.source_start,
                source_end=unit.source_end,
                source_text=unit.source_text,
                kind=kind,
                temporary_role_name=temporary_role_name,
                role_aliases=role_aliases,
                role_confidence=annotation.role_confidence,
                speak_enabled=speak_enabled,
            )
            for unit, annotation, (
                kind,
                temporary_role_name,
                role_aliases,
                speak_enabled,
            ) in zip(units, result.units, constrained, strict=True)
        )
    )
    validate_chunk_analysis(chunk, materialized)
    return materialized


def _constrain_unit_annotation(
    unit: ScriptAnalysisUnit,
    annotation: UnitAnalysis,
) -> tuple[
    Literal["dialogue", "narration", "stage_direction"],
    str | None,
    tuple[str, ...],
    bool,
]:
    if unit.context == "quoted_dialogue":
        return (
            "dialogue",
            annotation.temporary_role_name,
            annotation.role_aliases,
            True,
        )
    if unit.context == "quote_bridge_narration":
        return ("narration", None, (), True)
    if unit.context == "pause_marker":
        return ("stage_direction", None, (), False)
    return (
        annotation.kind,
        annotation.temporary_role_name,
        annotation.role_aliases,
        annotation.speak_enabled,
    )


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
