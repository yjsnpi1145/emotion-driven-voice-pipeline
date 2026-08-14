from __future__ import annotations

import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import AnalyzedUtterance, ChunkAnalysisResult
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.script_chunking import (
    split_script,
    validate_chunk_analysis,
)


def test_split_script_preserves_every_character() -> None:
    source = "第一幕\n\n旁白：夜色降临。\n甲：你好！\n乙：再见。"
    chunks = split_script(source, max_chars=12)
    assert "".join(item.source_text for item in chunks) == source
    assert chunks[0].source_start == 0
    assert chunks[-1].source_end == len(source)
    assert all(
        left.source_end == right.source_start
        for left, right in zip(chunks, chunks[1:], strict=False)
    )


def test_chunk_validation_rejects_rewritten_text() -> None:
    chunk = split_script("甲：你好。", max_chars=20)[0]
    result = ChunkAnalysisResult(
        utterances=(
            AnalyzedUtterance(
                source_start=0,
                source_end=len(chunk.source_text),
                source_text="乙：你好。",
                kind="dialogue",
                temporary_role_name="乙",
                role_confidence=0.9,
                speak_enabled=True,
            ),
        )
    )
    with pytest.raises(PipelineError) as exc:
        validate_chunk_analysis(chunk, result)
    assert exc.value.code == ErrorCode.LLM_INVALID_RESPONSE


@pytest.mark.asyncio
async def test_fake_director_runs_analysis_reconciliation_and_translation() -> None:
    director = FakeDirector()
    chunk = split_script("旁白。\n甲：你好。", max_chars=100)[0]
    analysis = await director.analyze_script_chunk(chunk=chunk)
    validate_chunk_analysis(chunk, analysis)
    cast = await director.reconcile_cast(utterances=analysis.utterances)
    assert {role.kind for role in cast.roles} == {"narrator", "character"}
    assert len(cast.assignments) == len(analysis.utterances)
