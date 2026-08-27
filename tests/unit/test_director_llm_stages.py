from __future__ import annotations

from itertools import pairwise

import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    ChunkAnalysisResult,
    ScriptChunk,
    UnitAnalysis,
    UnitAnalysisResult,
)
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.script_chunking import (
    build_analysis_units,
    materialize_unit_analysis,
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


@pytest.mark.parametrize(
    "source",
    [
        "第一幕\r\n\r\n旁白：夜色降临。\r\n甲：你好！  \n乙：再见。\t",
        "甲" * 200,
        " \r\n\t ",
    ],
)
def test_analysis_units_preserve_every_character_and_range(source: str) -> None:
    chunk = ScriptChunk(
        chunk_id="stable-chunk",
        source_start=37,
        source_end=37 + len(source),
        source_text=source,
    )

    units = build_analysis_units(chunk)

    assert "".join(unit.source_text for unit in units) == source
    assert units[0].source_start == chunk.source_start
    assert units[-1].source_end == chunk.source_end
    assert all(left.source_end == right.source_start for left, right in pairwise(units))
    assert all(len(unit.source_text) <= 160 for unit in units)
    assert [unit.unit_id for unit in units] == [
        f"{chunk.chunk_id}:u{index:04d}" for index in range(len(units))
    ]


def test_analysis_units_split_quoted_dialogue_from_inline_narration() -> None:
    source = "“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”"
    chunk = ScriptChunk(
        chunk_id="quoted-scene",
        source_start=0,
        source_end=len(source),
        source_text=source,
    )

    units = build_analysis_units(chunk)

    assert [(unit.source_text, unit.context) for unit in units] == [
        ("“我的初吻……”", "quoted_dialogue"),
        ("她慌乱地摆弄着手指，目光四处乱飘，", "quote_bridge_narration"),
        ("“祥子，为什么——”", "quoted_dialogue"),
    ]


@pytest.mark.parametrize(
    "source",
    [
        "前文「日文对白」后文",
        "前文『二重对白』后文",
        'Before "English dialogue" after',
    ],
)
def test_analysis_units_support_balanced_quote_styles(source: str) -> None:
    chunk = ScriptChunk(
        chunk_id="quote-styles",
        source_start=19,
        source_end=19 + len(source),
        source_text=source,
    )

    units = build_analysis_units(chunk)

    assert "".join(unit.source_text for unit in units) == source
    assert [unit.context for unit in units].count("quoted_dialogue") == 1
    assert units[0].source_start == chunk.source_start
    assert units[-1].source_end == chunk.source_end
    assert all(left.source_end == right.source_start for left, right in pairwise(units))


def test_unmatched_quote_falls_back_losslessly_to_general_units() -> None:
    source = "旁白。“这段引号没有闭合，因此不能推断为对白。"
    chunk = ScriptChunk(
        chunk_id="unmatched-quote",
        source_start=7,
        source_end=7 + len(source),
        source_text=source,
    )

    units = build_analysis_units(chunk)

    assert "".join(unit.source_text for unit in units) == source
    assert {unit.context for unit in units} == {"general"}


def test_long_quoted_dialogue_respects_the_analysis_unit_limit() -> None:
    source = "“" + "很长的对白" * 40 + "”"
    chunk = ScriptChunk(
        chunk_id="long-quote",
        source_start=0,
        source_end=len(source),
        source_text=source,
    )

    units = build_analysis_units(chunk)

    assert "".join(unit.source_text for unit in units) == source
    assert all(unit.context == "quoted_dialogue" for unit in units)
    assert all(len(unit.source_text) <= 160 for unit in units)


def test_unit_analysis_materializes_only_trusted_local_source_slices() -> None:
    chunk = ScriptChunk(
        chunk_id="regression-chunk",
        source_start=100,
        source_end=100 + len("旁白。\n甲：你好。"),
        source_text="旁白。\n甲：你好。",
    )
    units = build_analysis_units(chunk)
    annotations = UnitAnalysisResult(
        units=tuple(
            UnitAnalysis(
                unit_id=unit.unit_id,
                kind="narration" if index == 0 else "dialogue",
                temporary_role_name=None if index == 0 else "甲",
                role_aliases=(),
                role_confidence=0.9,
                speak_enabled=True,
            )
            for index, unit in enumerate(units)
        )
    )

    materialized = materialize_unit_analysis(chunk, units, annotations)

    assert "source_start" not in UnitAnalysis.model_fields
    assert "source_end" not in UnitAnalysis.model_fields
    assert "source_text" not in UnitAnalysis.model_fields
    assert [item.source_start for item in materialized.utterances] == [
        item.source_start for item in units
    ]
    assert [item.source_end for item in materialized.utterances] == [
        item.source_end for item in units
    ]
    assert [item.source_text for item in materialized.utterances] == [
        item.source_text for item in units
    ]
    assert "".join(item.source_text for item in materialized.utterances) == chunk.source_text


def test_unit_analysis_enforces_quote_context_classifications() -> None:
    source = "“第一句。”她低下头，“第二句。”"
    chunk = ScriptChunk(
        chunk_id="quote-constraints",
        source_start=0,
        source_end=len(source),
        source_text=source,
    )
    units = build_analysis_units(chunk)
    annotations = UnitAnalysisResult(
        units=tuple(
            UnitAnalysis(
                unit_id=unit.unit_id,
                kind="dialogue" if unit.context == "quote_bridge_narration" else "narration",
                temporary_role_name="错误角色",
                role_aliases=("错误别名",),
                role_confidence=0.72,
                speak_enabled=False,
            )
            for unit in units
        )
    )

    result = materialize_unit_analysis(chunk, units, annotations)

    assert [item.kind for item in result.utterances] == [
        "dialogue",
        "narration",
        "dialogue",
    ]
    assert [item.speak_enabled for item in result.utterances] == [True, True, True]
    assert result.utterances[0].temporary_role_name == "错误角色"
    assert result.utterances[1].temporary_role_name is None
    assert result.utterances[1].role_aliases == ()


@pytest.mark.parametrize("invalid_kind", ["missing", "duplicate", "reversed", "unknown"])
def test_unit_analysis_rejects_any_id_mismatch(invalid_kind: str) -> None:
    chunk = split_script("旁白。甲：你好。", max_chars=100)[0]
    units = build_analysis_units(chunk)
    valid = [
        UnitAnalysis(
            unit_id=unit.unit_id,
            kind="narration",
            temporary_role_name=None,
            role_aliases=(),
            role_confidence=0.9,
            speak_enabled=True,
        )
        for unit in units
    ]
    if invalid_kind == "missing":
        invalid = valid[:-1]
    elif invalid_kind == "duplicate":
        invalid = [valid[0], *valid]
    elif invalid_kind == "reversed":
        invalid = list(reversed(valid))
    else:
        invalid = [valid[0].model_copy(update={"unit_id": "unknown:u9999"}), *valid[1:]]

    with pytest.raises(PipelineError) as exc:
        materialize_unit_analysis(chunk, units, UnitAnalysisResult(units=tuple(invalid)))

    assert exc.value.code == ErrorCode.LLM_INVALID_RESPONSE
    assert "unit IDs" in exc.value.message


@pytest.mark.asyncio
async def test_fake_director_runs_analysis_reconciliation_and_translation() -> None:
    director = FakeDirector()
    chunk = split_script("旁白。\n甲：你好。", max_chars=100)[0]
    analysis = await director.analyze_script_chunk(chunk=chunk)
    validate_chunk_analysis(chunk, analysis)
    cast = await director.reconcile_cast(utterances=analysis.utterances)
    assert {role.kind for role in cast.roles} == {"narrator", "character"}
    assert len(cast.assignments) == len(analysis.utterances)
