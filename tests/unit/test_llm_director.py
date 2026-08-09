from __future__ import annotations

import hashlib

import pytest

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.modules.llm.director import validate_director_plan
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan


def _segment(
    start: int,
    end: int,
    *,
    ordinal: int,
    synthesis_text: str = "これは目標言語の本文です。",
    ref_text_cn: str = "我仍然保持冷静。",
) -> DirectedSegment:
    return DirectedSegment(
        ordinal=ordinal,
        source_start=start,
        source_end=end,
        emotion_description="冷静",
        emotion_vector=(0.0, 0.0, 0.1, 0.0, 0.0, 0.1, 0.0, 0.2),
        synthesis_text=synthesis_text,
        ref_text_cn=ref_text_cn,
        pause_after_ms=500,
        speed_factor=1.0,
        seed=1234,
    )


def test_validate_director_plan_uses_original_unicode_source_slices() -> None:
    source = "甲日本語乙"
    plan = DirectorPlan(
        source_text_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        segments=(
            _segment(0, 1, ordinal=0, synthesis_text="一つ目。"),
            _segment(1, 4, ordinal=1, synthesis_text="日本語の訳文。"),
            _segment(4, 5, ordinal=2, synthesis_text="三つ目。"),
        ),
    )

    materialized = validate_director_plan(source, plan)

    assert [item.source_text for item in materialized] == ["甲", "日本語", "乙"]
    assert [item.synthesis_text for item in materialized] == [
        "一つ目。",
        "日本語の訳文。",
        "三つ目。",
    ]


@pytest.mark.parametrize("invalid_reference", ["これは参考です。", "English reference only."])
def test_directed_segment_rejects_non_chinese_reference_text(invalid_reference: str) -> None:
    with pytest.raises(ValueError):
        _segment(0, 1, ordinal=0, ref_text_cn=invalid_reference)


def test_validate_director_plan_rejects_a_gap() -> None:
    source = "甲乙丙"
    plan = DirectorPlan(
        source_text_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        segments=(_segment(0, 1, ordinal=0), _segment(2, 3, ordinal=1)),
    )

    with pytest.raises(PipelineError, match="cover source text"):
        validate_director_plan(source, plan)
