from __future__ import annotations

from uuid import uuid4

import pytest

from voice_pipeline.modules.llm.activity import LlmActivityLog


@pytest.mark.asyncio
async def test_activity_snapshot_tracks_active_operation_until_terminal_event() -> None:
    activity = LlmActivityLog()
    operation_id = uuid4()

    await activity.emit(
        operation_id=operation_id,
        operation="chapter_plan",
        kind="started",
        message="开始规划章节",
    )
    waiting = await activity.snapshot()
    await activity.emit(
        operation_id=operation_id,
        operation="chapter_plan",
        kind="request_sent",
        message="请求已发送",
    )
    await activity.emit(
        operation_id=operation_id,
        operation="chapter_plan",
        kind="completed",
        message="规划完成",
        content='{"segments":[]}',
    )
    completed = await activity.snapshot()

    assert waiting.active is True
    assert waiting.active_operation == "chapter_plan"
    assert waiting.active_since_utc is not None
    assert completed.active is False
    assert completed.active_operation is None
    assert [event.kind for event in completed.events] == [
        "started",
        "request_sent",
        "completed",
    ]
    assert completed.events[-1].content == '{"segments":[]}'


@pytest.mark.asyncio
async def test_activity_log_is_bounded_and_truncates_large_output() -> None:
    activity = LlmActivityLog(max_events=3, max_content_chars=32)
    operation_id = uuid4()
    for index in range(5):
        await activity.emit(
            operation_id=operation_id,
            operation="reference_correction",
            kind="response",
            message=f"响应 {index}",
            content="x" * 100,
        )

    snapshot = await activity.snapshot()

    assert [event.sequence for event in snapshot.events] == [3, 4, 5]
    assert all(event.content is not None for event in snapshot.events)
    assert all(len(event.content or "") <= 32 for event in snapshot.events)
    assert all((event.content or "").endswith("[已截断]") for event in snapshot.events)


@pytest.mark.asyncio
async def test_failed_event_clears_active_state_and_serializes_without_secrets() -> None:
    activity = LlmActivityLog()
    operation_id = uuid4()
    await activity.emit(
        operation_id=operation_id,
        operation="connection_test",
        kind="started",
        message="测试连接",
    )
    await activity.emit(
        operation_id=operation_id,
        operation="connection_test",
        kind="failed",
        message="LLM_UNAVAILABLE",
    )

    payload = (await activity.snapshot()).model_dump(mode="json")

    assert payload["active"] is False
    assert payload["events"][-1]["kind"] == "failed"
    assert "api_key" not in str(payload).casefold()
    assert "authorization" not in str(payload).casefold()


@pytest.mark.asyncio
async def test_activity_accepts_script_preprocessing_operation() -> None:
    activity = LlmActivityLog()
    operation_id = uuid4()

    await activity.emit(
        operation_id=operation_id,
        operation="script_preprocessing",
        kind="started",
        message="开始文本预处理",
    )
    await activity.emit(
        operation_id=operation_id,
        operation="script_preprocessing",
        kind="completed",
        message="文本预处理完成",
        content='{"items":[]}',
    )

    snapshot = await activity.snapshot()
    assert snapshot.active is False
    assert [event.operation for event in snapshot.events] == [
        "script_preprocessing",
        "script_preprocessing",
    ]
