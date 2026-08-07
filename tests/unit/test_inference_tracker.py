from __future__ import annotations

from uuid import UUID

import pytest

from voice_pipeline.core.inference_tracker import InferenceTracker

JOB_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
JOB_B = UUID("bbbbbbbb-0000-4000-8000-000000000001")


@pytest.mark.asyncio
async def test_begin_sets_active_inference_to_one() -> None:
    tracker = InferenceTracker()
    lease = await tracker.begin("indextts", job_id=JOB_A)
    assert tracker.active_count("indextts") == 1
    assert tracker.current_job("indextts") == JOB_A
    await lease.confirm_completed()


@pytest.mark.asyncio
async def test_confirm_completed_clears_active() -> None:
    tracker = InferenceTracker()
    lease = await tracker.begin("indextts", job_id=JOB_A)
    await lease.confirm_completed()
    assert tracker.active_count("indextts") == 0
    assert not tracker.is_unknown("indextts")


@pytest.mark.asyncio
async def test_confirm_aborted_clears_active() -> None:
    tracker = InferenceTracker()
    lease = await tracker.begin("indextts", job_id=JOB_A)
    await lease.confirm_aborted()
    assert tracker.active_count("indextts") == 0
    assert not tracker.is_unknown("indextts")


@pytest.mark.asyncio
async def test_mark_unknown_clears_active_and_flags_unknown() -> None:
    tracker = InferenceTracker()
    lease = await tracker.begin("indextts", job_id=JOB_A)
    await lease.mark_unknown()
    assert tracker.active_count("indextts") == 0
    assert tracker.is_unknown("indextts")


@pytest.mark.asyncio
async def test_duplicate_begin_is_internal_error() -> None:
    tracker = InferenceTracker()
    first = await tracker.begin("indextts", job_id=JOB_A)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            await tracker.begin("indextts", job_id=JOB_B)
    finally:
        await first.confirm_completed()


@pytest.mark.asyncio
async def test_engines_are_independent() -> None:
    tracker = InferenceTracker()
    index_lease = await tracker.begin("indextts", job_id=JOB_A)
    gsv_lease = await tracker.begin("gpt_sovits", job_id=JOB_B)
    assert tracker.active_count("indextts") == 1
    assert tracker.active_count("gpt_sovits") == 1
    await index_lease.confirm_completed()
    assert tracker.active_count("indextts") == 0
    assert tracker.active_count("gpt_sovits") == 1
    await gsv_lease.confirm_completed()
    assert tracker.active_count("gpt_sovits") == 0


@pytest.mark.asyncio
async def test_begin_after_unknown_is_allowed() -> None:
    tracker = InferenceTracker()
    lease = await tracker.begin("indextts", job_id=JOB_A)
    await lease.mark_unknown()
    # a fresh inference may begin again
    lease2 = await tracker.begin("indextts", job_id=JOB_B)
    await lease2.confirm_completed()
