from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.job_executor import JobExecutor
from voice_pipeline.models.persistence import (
    DispatcherStats,
    JsonValue,
    PersistentJobRecord,
)
from voice_pipeline.storage.job_store import SqliteJobStore


class DurableJobDispatcher:
    """One durable FIFO claim loop over SQLite-backed generation jobs."""

    def __init__(
        self,
        *,
        store: SqliteJobStore,
        queue: SerialGpuQueue,
        executor: JobExecutor,
        instance_id: UUID,
        queue_timeout_seconds: float,
    ) -> None:
        self._store = store
        self._queue = queue
        self._executor = executor
        self._instance_id = instance_id
        self._queue_timeout_seconds = queue_timeout_seconds
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._active: dict[UUID, asyncio.Task[None]] = {}
        self._state: str = "stopped"
        self._queued_count = 0
        self._recovered_interrupted_count = 0

    async def start(self) -> None:
        if self._state == "running":
            return
        if self._state != "stopped":
            raise RuntimeError("dispatcher is already stopping")
        recovery = await self._store.recover_interrupted()
        self._recovered_interrupted_count = len(recovery.interrupted_job_ids)
        self._state = "running"
        self._loop_task = asyncio.create_task(self._run(), name="durable-job-dispatcher")
        await self.notify()

    async def notify(self) -> None:
        if self._state == "running":
            self._wake.set()

    async def cancel(self, job_id: UUID) -> PersistentJobRecord:
        record = await self._store.cancel(job_id)
        if record.status == "running":
            active = self._active.get(job_id)
            if active is not None:
                active.cancel()
        await self.notify()
        return await self._store.get(job_id)

    async def stop(self, *, deadline: float) -> None:
        if self._state == "stopped":
            return
        self._state = "stopping"
        self._wake.set()
        for task in self._active.values():
            task.cancel()
        tasks = [task for task in [self._loop_task, *self._active.values()] if task is not None]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
            except TimeoutError:
                pass
        self._active.clear()
        self._loop_task = None
        self._state = "stopped"

    def stats(self) -> DispatcherStats:
        return DispatcherStats(
            state=self._state,  # type: ignore[arg-type]
            queued_count=self._queued_count,
            active_job_id=next(iter(self._active), None),
            recovered_interrupted_count=self._recovered_interrupted_count,
        )

    async def _run(self) -> None:
        try:
            while self._state == "running":
                # Clearing before the DB query prevents a submission between
                # query and wait from being lost.
                self._wake.clear()
                records = await self._store.list_queued(limit=32)
                self._queued_count = len(records)
                if not records:
                    await self._wake.wait()
                    continue
                for candidate in records:
                    if self._state != "running":
                        return
                    if not await self._store.claim(candidate.job_id, instance_id=self._instance_id):
                        continue
                    record = await self._store.get(candidate.job_id)
                    task = asyncio.create_task(
                        self._execute_claimed(record),
                        name=f"persistent-job-{record.job_id}",
                    )
                    self._active[record.job_id] = task
                    try:
                        await task
                    finally:
                        self._active.pop(record.job_id, None)
        except asyncio.CancelledError:
            raise

    async def _execute_claimed(self, record: PersistentJobRecord) -> None:
        try:
            remaining_queue_wait = (
                self._queue_timeout_seconds
                - (datetime.now(UTC) - record.created_at_utc).total_seconds()
            )
            result = await self._queue.run(
                lambda: self._executor.execute(record),
                queue_timeout_seconds=remaining_queue_wait,
            )
        except asyncio.CancelledError:
            current = await self._store.get(record.job_id)
            if current.cancel_requested_at_utc is not None:
                await self._store.mark_cancelled(record.job_id, error=_cancel_error("running"))
            else:
                await self._store.mark_failed(record.job_id, error=_interrupted_error())
        except PipelineError as exc:
            await self._store.mark_failed(record.job_id, error=exc.as_dict())
        except Exception:
            await self._store.mark_failed(record.job_id, error=_internal_error())
        else:
            committed = await self._store.mark_succeeded(record.job_id, result=result)
            if not committed:
                current = await self._store.get(record.job_id)
                if current.cancel_requested_at_utc is not None:
                    await self._store.mark_cancelled(record.job_id, error=_cancel_error("running"))


def _cancel_error(state: str) -> dict[str, JsonValue]:
    return {
        "code": "JOB_CANCELLED",
        "stage": "jobs",
        "message": f"job was cancelled while {state}",
        "retryable": False,
        "details": {"state": state},
    }


def _interrupted_error() -> dict[str, JsonValue]:
    return {
        "code": ErrorCode.ENGINE_UNAVAILABLE.value,
        "stage": "dispatcher",
        "message": "job dispatcher stopped before completion",
        "retryable": True,
        "details": {},
    }


def _internal_error() -> dict[str, JsonValue]:
    return {
        "code": "INTERNAL_ERROR",
        "stage": "dispatcher",
        "message": "unhandled execution failure",
        "retryable": False,
        "details": {},
    }
