from __future__ import annotations

import pytest

from voice_pipeline.modules.llm.fake import FakeDirector


@pytest.mark.asyncio
async def test_fake_director_covers_source_and_uses_chinese_reference_text() -> None:
    director = FakeDirector()
    source = "第一句。第二句。"

    plan = await director.create_plan(source_text=source, target_language="ja")

    assert [(item.source_start, item.source_end) for item in plan.segments] == [(0, 4), (4, 8)]
    assert all(item.ref_text_cn for item in plan.segments)
    assert all(sum(item.emotion_vector) <= 0.8 for item in plan.segments)
