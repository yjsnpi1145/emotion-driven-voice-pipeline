from __future__ import annotations

import hashlib

from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan


class FakeDirector:
    """A deterministic source-range director used by local CPU tests."""

    async def create_plan(self, *, source_text: str, target_language: LanguageCode) -> DirectorPlan:
        del target_language
        boundaries = _boundaries(source_text)
        return DirectorPlan(
            source_text_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            segments=tuple(
                DirectedSegment(
                    ordinal=ordinal,
                    source_start=start,
                    source_end=end,
                    emotion_description="平静、克制",
                    emotion_vector=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
                    ref_text_cn="我仍然保持冷静。",
                    pause_after_ms=500,
                    speed_factor=1.0,
                    seed=1234 + ordinal,
                )
                for ordinal, (start, end) in enumerate(boundaries)
            ),
        )

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: str,
        emotion_description: str,
    ) -> str:
        del direction, emotion_description
        return current


def _boundaries(source_text: str) -> list[tuple[int, int]]:
    boundaries: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(source_text, start=1):
        if character in "。！？.!?\n" or index - start >= 40:
            boundaries.append((start, index))
            start = index
    if start < len(source_text):
        boundaries.append((start, len(source_text)))
    return boundaries
