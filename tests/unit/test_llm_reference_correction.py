from __future__ import annotations

from dataclasses import dataclass

import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.llm.director import ReferenceTextDirector
from voice_pipeline.modules.llm.models import DirectedSegment


@dataclass
class SequenceProbe:
    durations: list[float]

    async def generate_and_measure(self, text: str, vector: tuple[float, ...], seed: int) -> float:
        del text, vector, seed
        return self.durations.pop(0)


@dataclass
class Corrections:
    values: list[str]

    async def correct_reference_text(
        self, *, current: str, direction: str, emotion_description: str
    ) -> str:
        del current, direction, emotion_description
        return self.values.pop(0)


def _segment() -> DirectedSegment:
    return DirectedSegment(
        ordinal=0,
        source_start=0,
        source_end=1,
        emotion_description="空洞、悲伤",
        emotion_vector=(0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.2),
        synthesis_text="これは目標言語の本文です。",
        ref_text_cn="初始参考文本。",
        pause_after_ms=0,
        speed_factor=1.0,
        seed=1234,
    )


@pytest.mark.asyncio
async def test_reference_correction_changes_only_text_until_duration_is_in_range() -> None:
    director = ReferenceTextDirector(Corrections(["过长参考文本。", "最终参考文本。"]))

    resolved = await director.resolve_reference_text(
        _segment(), SequenceProbe([2.2, 10.1, 4.0]), max_corrections=2
    )

    assert resolved.ref_text_cn == "最终参考文本。"
    assert resolved.emotion_vector == _segment().emotion_vector
    assert resolved.source_start == 0
    assert resolved.reference_corrections == 2


@pytest.mark.asyncio
async def test_reference_duration_between_nine_and_ten_needs_no_correction() -> None:
    director = ReferenceTextDirector(Corrections([]))

    resolved = await director.resolve_reference_text(
        _segment(), SequenceProbe([9.358]), max_corrections=2
    )

    assert resolved.ref_text_cn == "初始参考文本。"
    assert resolved.reference_corrections == 0


@pytest.mark.asyncio
async def test_reference_correction_reports_an_explicit_duration_error_after_budget() -> None:
    director = ReferenceTextDirector(Corrections(["仍然短。", "仍然短。"]))

    with pytest.raises(PipelineError) as captured:
        await director.resolve_reference_text(
            _segment(), SequenceProbe([2.0, 2.1, 2.2]), max_corrections=2
        )

    assert captured.value.code == ErrorCode.REFERENCE_DURATION_INVALID
