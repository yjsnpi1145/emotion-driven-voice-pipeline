from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import RuntimeHealth

T = TypeVar("T")


@dataclass
class QueueItem(Generic[T]):
    factory: Callable[[], Awaitable[T]]
    future: asyncio.Future[T]
    started: asyncio.Future[None]
    enqueued_at: float
    cancelled: bool = field(default=False)


class SerialGpuQueue:
    """Single-consumer queue: every GPU command executes in one consumer."""

    def __init__(self, queue_timeout_seconds: float) -> None:
        self._items: asyncio.Queue[QueueItem[Any] | None] = asyncio.Queue()
        self._consumer: asyncio.Task[None] | None = None
        self._active_count = 0
        self._max_active_observed = 0
        self._queue_timeout_seconds = queue_timeout_seconds
        self._state: str = "stopped"
        self._poison_reason: str | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    async def start(self) -> None:
        if self._consumer is None:
            self._state = "accepting"
            self._consumer = asyncio.create_task(self._consume(), name="single-gpu-consumer")

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self._consumer is None or self._state == "stopped":
            raise RuntimeError("GPU queue is not started")
        if self._state != "accepting":
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "queue",
                self._poison_reason or "GPU queue is not accepting work",
                retryable=False,
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        started: asyncio.Future[None] = loop.create_future()
        item = QueueItem(
            factory=factory,
            future=future,
            started=started,
            enqueued_at=loop.time(),
        )
        await self._items.put(item)
        try:
            await asyncio.wait_for(
                asyncio.shield(started),
                timeout=self._queue_timeout_seconds,
            )
        except TimeoutError as exc:
            if not started.done():
                item.cancelled = True
                if not future.done():
                    future.cancel()
                raise PipelineError(
                    ErrorCode.QUEUE_TIMEOUT,
                    "queue",
                    "job expired before GPU execution",
                    retryable=True,
                ) from exc
        try:
            return await future
        except asyncio.CancelledError:
            item.cancelled = True
            raise

    async def _consume(self) -> None:
        while True:
            item = await self._items.get()
            if item is None:
                self._items.task_done()
                return
            if item.cancelled or item.future.cancelled():
                self._items.task_done()
                continue
            entered = False
            try:
                item.started.set_result(None)
                self._active_count += 1
                if self._active_count == 1:
                    self._idle_event.clear()
                entered = True
                self._max_active_observed = max(self._max_active_observed, self._active_count)
                result = await item.factory()
                if not item.future.cancelled():
                    item.future.set_result(result)
            except asyncio.CancelledError:
                if not item.future.cancelled():
                    item.future.set_exception(
                        PipelineError(
                            ErrorCode.ENGINE_UNAVAILABLE,
                            "queue",
                            "GPU consumer cancelled",
                            retryable=False,
                        )
                    )
                raise
            except BaseException as exc:
                if isinstance(exc, PipelineError) and exc.poison_queue:
                    self.poison(exc.message)
                if not item.future.cancelled():
                    item.future.set_exception(exc)
            finally:
                if entered:
                    self._active_count -= 1
                    if self._active_count == 0:
                        self._idle_event.set()
                self._items.task_done()

    def poison(self, reason: str) -> None:
        """Fail-closed: reject new work and fail all queued-but-not-started items."""
        self._state = "poisoned"
        self._poison_reason = reason
        while True:
            try:
                item = self._items.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._items.task_done()
            if item is None:
                continue
            if not item.started.done() and not item.future.cancelled():
                item.cancelled = True
                if not item.future.done():
                    item.future.set_exception(
                        PipelineError(
                            ErrorCode.ENGINE_UNAVAILABLE,
                            "queue",
                            reason,
                            retryable=False,
                        )
                    )
                if not item.started.done():
                    item.started.set_result(None)

    def resume_after_verified_recovery(self, health: RuntimeHealth) -> None:
        if self._state != "poisoned":
            raise ValueError("queue is not poisoned; nothing to resume")
        if health.status != "ready":
            raise ValueError("cannot resume: health is not ready")
        for engine in ("indextts", "gpt_sovits"):
            worker = getattr(health.workers, engine)
            if worker.active_inference != 0:
                raise ValueError(f"cannot resume: {engine} active inference is not zero")
            if worker.state in ("unknown", "unhealthy"):
                raise ValueError(f"cannot resume: {engine} is {worker.state}")
        self._state = "accepting"
        self._poison_reason = None

    async def stop(
        self,
        *,
        deadline: float | None = None,
        grace_seconds: float = 0.5,
        abort_active: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        if self._consumer is None or self._state == "stopped":
            self._state = "stopped"
            return
        if deadline is None:
            deadline = loop.time() + 5.0
        self._state = "stopping"
        # Fail every queued-but-not-started item.
        while True:
            try:
                item = self._items.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._items.task_done()
            if item is None:
                continue
            if not item.started.done() and not item.future.cancelled():
                item.cancelled = True
                if not item.future.done():
                    item.future.set_exception(
                        PipelineError(
                            ErrorCode.ENGINE_UNAVAILABLE,
                            "queue",
                            "control plane shutting down",
                            retryable=False,
                        )
                    )
                if not item.started.done():
                    item.started.set_result(None)
        # Give the active factory a bounded grace.
        if self._active_count > 0 and grace_seconds > 0:
            wait = min(grace_seconds, max(0.0, deadline - loop.time()))
            if wait > 0:
                try:
                    await asyncio.wait_for(self._wait_active_zero(), timeout=wait)
                except TimeoutError:
                    pass
        # Ask the caller to abort active inference with the same deadline.
        if self._active_count > 0 and abort_active is not None:
            try:
                await abort_active(deadline)
            except Exception:
                pass
        # Confirm zero activity with a short bounded settle window after abort.
        if self._active_count > 0:
            settle = min(0.5, max(0.0, deadline - loop.time()))
            if settle > 0:
                try:
                    await asyncio.wait_for(self._wait_active_zero(), timeout=settle)
                except TimeoutError:
                    pass
        # Cancel and await the single consumer (forces the factory to unwind).
        consumer = self._consumer
        if consumer is not None and not consumer.done():
            consumer.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(consumer),
                    timeout=max(0.0, deadline - loop.time()),
                )
            except (asyncio.CancelledError, TimeoutError):
                pass
        self._state = "stopped"

    async def _wait_active_zero(self) -> None:
        await self._idle_event.wait()

    def stats(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            state=self._state,
            poison_reason=self._poison_reason,
            active_count=self._active_count,
            queued_count=self._items.qsize(),
            max_active_observed=self._max_active_observed,
            max_concurrency=1,
        )
