from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID

from voice_pipeline.models.schemas import EngineFingerprint, WorkerName

_KNOWN_ENGINES: tuple[WorkerName, ...] = ("indextts", "gpt_sovits")


def fake_fingerprint(engine: WorkerName) -> EngineFingerprint:
    """Deterministic, fully-populated fake fingerprint for in-process clients."""
    source = "in-process-fake"

    def h(field: str) -> str:
        return hashlib.sha256(f"{engine}:{source}:{field}".encode()).hexdigest()

    return EngineFingerprint(
        schema_version=1,
        engine=engine,
        source_revision=source,
        model_revision="1",
        engine_lock_sha256=h("engine-lock"),
        checkpoint_lock_sha256=h("checkpoint-lock"),
        environment_lock_sha256=h("environment-lock"),
        runtime_config_sha256=h("runtime-config"),
    )


class TrackerLease:
    """Lease handed out by :class:`InferenceTracker`; the control plane's
    authoritative begin/terminal-transition object."""

    def __init__(self, tracker: InferenceTracker, engine: WorkerName) -> None:
        self._tracker = tracker
        self._engine = engine

    async def confirm_completed(self) -> None:
        await self._tracker._finish(self._engine, unknown=False)

    async def confirm_aborted(self) -> None:
        await self._tracker._finish(self._engine, unknown=False)

    async def mark_unknown(self) -> None:
        await self._tracker._finish(self._engine, unknown=True)


class InferenceTracker:
    """Tracks active inference per engine; at most one lease per engine."""

    def __init__(self) -> None:
        self._locks: dict[WorkerName, asyncio.Lock] = {
            engine: asyncio.Lock() for engine in _KNOWN_ENGINES
        }
        self._active: dict[WorkerName, bool] = {engine: False for engine in _KNOWN_ENGINES}
        self._unknown: dict[WorkerName, bool] = {engine: False for engine in _KNOWN_ENGINES}
        self._jobs: dict[WorkerName, UUID | None] = {engine: None for engine in _KNOWN_ENGINES}

    async def begin(self, engine: WorkerName, *, job_id: UUID) -> TrackerLease:
        async with self._locks[engine]:
            if self._active[engine]:
                raise RuntimeError(f"inference already active for {engine}")
            self._active[engine] = True
            self._jobs[engine] = job_id
        return TrackerLease(self, engine)

    async def _finish(self, engine: WorkerName, *, unknown: bool) -> None:
        async with self._locks[engine]:
            self._active[engine] = False
            self._jobs[engine] = None
            if unknown:
                self._unknown[engine] = True

    def active_count(self, engine: WorkerName) -> int:
        return 1 if self._active[engine] else 0

    def current_job(self, engine: WorkerName) -> UUID | None:
        return self._jobs[engine]

    def is_unknown(self, engine: WorkerName) -> bool:
        return self._unknown[engine]
