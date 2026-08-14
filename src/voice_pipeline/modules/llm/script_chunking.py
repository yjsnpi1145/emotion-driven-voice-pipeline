from __future__ import annotations

import hashlib

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import ChunkAnalysisResult, ScriptChunk

_SAFE_BOUNDARIES = frozenset("。！？.!?；;\n")


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
