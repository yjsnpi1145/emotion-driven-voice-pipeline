from __future__ import annotations

import asyncio

import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.gpu_queue import SerialGpuQueue


async def wait_until(predicate, wait_seconds: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("wait_until timed out")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_queue_never_runs_two_gpu_calls_at_once() -> None:
    active = 0
    max_active = 0

    async def work(value: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return value

    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    try:
        results = await asyncio.gather(*(queue.run(lambda i=i: work(i)) for i in range(6)))
    finally:
        await queue.stop()

    assert results == list(range(6))
    assert max_active == 1
    assert queue.stats().max_active_observed == 1


@pytest.mark.asyncio
async def test_failure_releases_queue_for_next_job() -> None:
    async def failing_work() -> None:
        raise RuntimeError("boom")

    async def successful_work(value: str) -> str:
        return value

    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await queue.run(failing_work)
        assert await queue.run(lambda: successful_work("ok")) == "ok"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_waiting_job_times_out_by_wall_clock_without_running_factory() -> None:
    release_first = asyncio.Event()
    second_called = False
    queue = SerialGpuQueue(queue_timeout_seconds=0.2)
    await queue.start()

    async def first() -> None:
        await release_first.wait()

    async def second() -> None:
        nonlocal second_called
        second_called = True

    first_task = asyncio.create_task(queue.run(first))
    await wait_until(lambda: queue.stats().active_count == 1)
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(PipelineError, match="QUEUE_TIMEOUT"):
            await queue.run(second)
        assert asyncio.get_running_loop().time() - started < 1.0
        assert second_called is False
    finally:
        release_first.set()
        await first_task
        await queue.stop()


@pytest.mark.asyncio
async def test_abort_failure_poisons_queue_and_never_runs_next_factory() -> None:
    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    next_called = False

    async def uncertain_failure() -> None:
        raise PipelineError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "runtime",
            "abort could not be confirmed",
            retryable=False,
            poison_queue=True,
        )

    async def next_work() -> None:
        nonlocal next_called
        next_called = True

    try:
        with pytest.raises(PipelineError, match="ENGINE_UNAVAILABLE"):
            await queue.run(uncertain_failure)
        assert queue.stats().state == "poisoned"
        with pytest.raises(PipelineError, match="ENGINE_UNAVAILABLE"):
            await queue.run(next_work)
        assert next_called is False
    finally:
        await queue.stop()


def _ready_health(active_indextts: int = 0, active_gsv: int = 0) -> object:
    from voice_pipeline.models.schemas import (
        EngineFingerprint,
        RuntimeHealth,
        WorkerHealth,
        WorkersHealth,
    )

    def fingerprint(engine: str) -> EngineFingerprint:
        return EngineFingerprint(
            schema_version=1,
            engine=engine,  # type: ignore[arg-type]
            source_revision="x",
            model_revision="1",
            engine_lock_sha256="0" * 64,
            checkpoint_lock_sha256="0" * 64,
            environment_lock_sha256="0" * 64,
            runtime_config_sha256="0" * 64,
        )

    def worker(engine: str, active: int) -> WorkerHealth:
        return WorkerHealth(
            state="ready",
            pid=1,
            create_time=1.0,
            python_executable=__import__("pathlib").Path("python.exe"),
            python_version="3.11",
            source_revision="x",
            fingerprint=fingerprint(engine),
            preflight_ok=True,
            active_inference=active,
        )

    return RuntimeHealth(
        status="ready",
        workers=WorkersHealth(
            indextts=worker("indextts", active_indextts),
            gpt_sovits=worker("gpt_sovits", active_gsv),
        ),
    )


@pytest.mark.asyncio
async def test_poison_only_resumes_after_verified_zero_activity_recovery() -> None:
    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()

    async def uncertain_failure() -> None:
        raise PipelineError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "runtime",
            "abort could not be confirmed",
            retryable=False,
            poison_queue=True,
        )

    async def ok_work(value: str) -> str:
        return value

    try:
        with pytest.raises(PipelineError, match="ENGINE_UNAVAILABLE"):
            await queue.run(uncertain_failure)
        assert queue.stats().state == "poisoned"

        # unverified health must NOT resume the queue
        bad_health = _ready_health(active_indextts=1)
        with pytest.raises(ValueError):
            queue.resume_after_verified_recovery(bad_health)
        assert queue.stats().state == "poisoned"

        # verified zero-activity health resumes
        queue.resume_after_verified_recovery(_ready_health())
        assert queue.stats().state == "accepting"
        assert await queue.run(lambda: ok_work("ok")) == "ok"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_stop_fails_queued_items_and_reaches_stopped() -> None:
    release = asyncio.Event()

    async def first() -> None:
        await release.wait()

    async def second() -> None:
        raise AssertionError("second factory must never run")

    queue = SerialGpuQueue(queue_timeout_seconds=5)
    await queue.start()
    first_task = asyncio.create_task(queue.run(first))
    await wait_until(lambda: queue.stats().active_count == 1)
    second_task = asyncio.create_task(queue.run(second))
    await asyncio.sleep(0.05)

    await queue.stop()

    assert queue.stats().state == "stopped"
    assert queue.stats().active_count == 0
    with pytest.raises(PipelineError):
        await second_task
    with pytest.raises(PipelineError):
        await first_task
