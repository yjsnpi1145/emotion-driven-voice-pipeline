from __future__ import annotations

import hashlib

import pytest

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.modules.llm.director import validate_director_plan
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan


def _segment(start: int, end: int, *, ordinal: int) -> DirectedSegment:
    return DirectedSegment(
        ordinal=ordinal,
        source_start=start,
        source_end=end,
        emotion_description="冷静",
        emotion_vector=(0.0, 0.0, 0.1, 0.0, 0.0, 0.1, 0.0, 0.2),
        ref_text_cn="我仍然保持冷静。",
        pause_after_ms=500,
        speed_factor=1.0,
        seed=1234,
    )


def test_validate_director_plan_uses_original_unicode_source_slices() -> None:
    source = "甲日本語乙"
    plan = DirectorPlan(
        source_text_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        segments=(
            _segment(0, 1, ordinal=0),
            _segment(1, 4, ordinal=1),
            _segment(4, 5, ordinal=2),
        ),
    )

    materialized = validate_director_plan(source, plan)

    assert [item.source_text for item in materialized] == ["甲", "日本語", "乙"]
    assert [item.synthesis_text for item in materialized] == ["甲", "日本語", "乙"]


def test_validate_director_plan_rejects_a_gap() -> None:
    source = "甲乙丙"
    plan = DirectorPlan(
        source_text_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        segments=(_segment(0, 1, ordinal=0), _segment(2, 3, ordinal=1)),
    )

    with pytest.raises(PipelineError, match="cover source text"):
        validate_director_plan(source, plan)
