from __future__ import annotations

from uuid import uuid4

import pytest

from voice_pipeline.core.jobs import InMemoryJobRegistry


@pytest.mark.asyncio
async def test_create_generates_unique_job_ids_for_same_request_id(tmp_path) -> None:
    registry = InMemoryJobRegistry(jobs_root=tmp_path / "jobs")
    rid = uuid4()
    ctx1 = await registry.create(request_id=rid, kind="segment", request_snapshot={"a": 1})
    ctx2 = await registry.create(request_id=rid, kind="segment", request_snapshot={"a": 1})
    assert ctx1.job_id != ctx2.job_id
    assert ctx1.job_dir != ctx2.job_dir
    assert ctx1.job_dir.parent == (tmp_path / "jobs").resolve()


@pytest.mark.asyncio
async def test_status_transitions(tmp_path) -> None:
    registry = InMemoryJobRegistry(jobs_root=tmp_path / "jobs")
    ctx = await registry.create(request_id=uuid4(), kind="reference", request_snapshot={})
    assert (await registry.get(ctx.job_id)).status == "queued"
    assert (await registry.get(ctx.job_id)).stage == "queued"

    await registry.mark_running(ctx.job_id)
    record = await registry.get(ctx.job_id)
    assert record.status == "running"
    assert record.started_at is not None

    await registry.mark_succeeded(ctx.job_id, result={"ok": True})
    record = await registry.get(ctx.job_id)
    assert record.status == "succeeded"
    assert record.result == {"ok": True}
    assert record.finished_at is not None


@pytest.mark.asyncio
async def test_mark_failed_stores_error(tmp_path) -> None:
    registry = InMemoryJobRegistry(jobs_root=tmp_path / "jobs")
    ctx = await registry.create(request_id=uuid4(), kind="gsv", request_snapshot={})
    await registry.mark_failed(ctx.job_id, error={"code": "INDEX_ENGINE_ERROR", "stage": "index"})
    record = await registry.get(ctx.job_id)
    assert record.status == "failed"
    assert record.error == {"code": "INDEX_ENGINE_ERROR", "stage": "index"}


@pytest.mark.asyncio
async def test_get_returns_independent_copies(tmp_path) -> None:
    registry = InMemoryJobRegistry(jobs_root=tmp_path / "jobs")
    ctx = await registry.create(request_id=uuid4(), kind="gsv", request_snapshot={"x": 1})
    r1 = await registry.get(ctx.job_id)
    r2 = await registry.get(ctx.job_id)
    assert r1 is not r2
    assert r1.request_snapshot == {"x": 1}
    # registry contents are unchanged regardless of caller-side copies
    assert (await registry.get(ctx.job_id)).status == "queued"


@pytest.mark.asyncio
async def test_unknown_job_raises_key_error(tmp_path) -> None:
    registry = InMemoryJobRegistry(jobs_root=tmp_path / "jobs")
    with pytest.raises(KeyError):
        await registry.get(uuid4())
