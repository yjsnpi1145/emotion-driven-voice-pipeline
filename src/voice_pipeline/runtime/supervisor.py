from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.inference_tracker import InferenceTracker, TrackerLease
from voice_pipeline.models.schemas import (
    EngineFingerprint,
    EngineIdentity,
    RuntimeHealth,
    WorkerHealth,
    WorkerName,
    WorkersHealth,
)

_ENGINES: tuple[WorkerName, ...] = ("indextts", "gpt_sovits")


class ProcessSupervisor:
    """Lifecycle owner for the two real worker processes.

    ``processes`` is an injectable process manager (the real
    :class:`voice_pipeline.runtime.process.RealWorkerProcessManager` in
    production, a fake with real child processes in tests).
    """

    def __init__(
        self,
        *,
        mode: str,
        processes: Any,
        fingerprints: dict[WorkerName, EngineFingerprint] | None = None,
        tracker: InferenceTracker | None = None,
        registry_path: Path | None = None,
        instance_id: str | None = None,
        audit_log: Path | None = None,
        control_pid: int | None = None,
        control_create_time: float | None = None,
        engine_lifecycle: str | None = None,
    ) -> None:
        self._mode = mode
        self._processes = processes
        self._fingerprints = fingerprints
        self._tracker = tracker or InferenceTracker()
        self._registry_path = registry_path
        self._instance_id = instance_id or str(uuid.uuid4())
        self._audit_log = audit_log
        self._control_pid = control_pid
        self._control_create_time = control_create_time
        self._engine_lifecycle = engine_lifecycle or mode
        self._python_version = sys.version.split()[0]
        self._ready: dict[WorkerName, bool] = {
            "indextts": False,
            "gpt_sovits": False,
        }
        if self._fingerprints is None:
            from voice_pipeline.core.inference_tracker import fake_fingerprint

            self._fingerprints = {
                "indextts": fake_fingerprint("indextts"),
                "gpt_sovits": fake_fingerprint("gpt_sovits"),
            }
        set_state_change_callback = getattr(
            self._processes,
            "set_state_change_callback",
            None,
        )
        if callable(set_state_change_callback):
            set_state_change_callback(self._write_registry)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._mode == "resident":
            for engine in ("indextts", "gpt_sovits"):
                if not self._processes.running_engine(engine):
                    await self._processes.start_engine(engine)
                self._ready[engine] = True
        self._write_registry()

    async def stop(self, *, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + 5.0
        for engine in ("indextts", "gpt_sovits"):
            if self._processes.running_engine(engine):
                await self._processes.stop_engine(engine, deadline=deadline)
            self._ready[engine] = False
        self._write_registry()

    async def ensure_engine(self, engine: WorkerName) -> None:
        self._require_known(engine)
        if self._mode == "exclusive_process":
            other: WorkerName = "gpt_sovits" if engine == "indextts" else "indextts"
            if self._processes.running_engine(other):
                await self._processes.stop_engine(other, deadline=None)
                self._ready[other] = False
                self._write_registry()
        if not self._processes.running_engine(engine):
            await self._processes.start_engine(engine)
        self._ready[engine] = True
        self._write_registry()

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None:
        self._require_known(engine)
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + 5.0
        await self._processes.stop_engine(engine, deadline=deadline)
        self._ready[engine] = False
        self._write_registry()
        if self._processes.running_engine(engine):
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} process tree could not be stopped",
                retryable=False,
                poison_queue=True,
            )

    # ------------------------------------------------------------------ #
    # identity / health
    # ------------------------------------------------------------------ #

    def engine_identity(self, engine: WorkerName) -> EngineIdentity:
        self._require_known(engine)
        identity = self._processes.engine_identity(engine)
        if identity is None or not self._processes.running_engine(engine):
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} is not ready",
                retryable=False,
            )
        return cast(EngineIdentity, identity)

    def fingerprint(self, engine: WorkerName) -> EngineFingerprint:
        self._require_known(engine)
        fp = self._fingerprints
        assert fp is not None
        return fp[engine]

    def health(self) -> RuntimeHealth:
        workers: dict[str, WorkerHealth] = {}
        for engine in _ENGINES:
            workers[engine] = self._worker_health(engine)
        states = [workers["indextts"].state, workers["gpt_sovits"].state]
        if any(state in ("unknown", "unhealthy") for state in states):
            status: str = "degraded"
        elif self._mode == "resident":
            status = "ready" if all(state == "ready" for state in states) else "degraded"
        else:
            ready = sum(1 for state in states if state == "ready")
            stopped = sum(1 for state in states if state == "stopped_expected")
            status = "ready" if ready <= 1 and ready + stopped == 2 else "degraded"
        return RuntimeHealth(
            status=status,  # type: ignore[arg-type]
            workers=WorkersHealth(indextts=workers["indextts"], gpt_sovits=workers["gpt_sovits"]),
        )

    def _worker_health(self, engine: WorkerName) -> WorkerHealth:
        identity = self._processes.engine_identity(engine)
        active = self._tracker.active_count(engine)
        fp = self._fingerprints
        assert fp is not None
        if identity is not None and self._processes.running_engine(engine):
            if self._ready[engine]:
                state = "unknown" if self._tracker.is_unknown(engine) else "ready"
            else:
                state = "starting"
            pid = identity.pid
            create_time = identity.create_time
            source = fp[engine].source_revision
        elif self._ready[engine]:
            state = "starting"
            pid = identity.pid if identity else None
            create_time = identity.create_time if identity else None
            source = fp[engine].source_revision
        else:
            state = "stopped_expected"
            pid = None
            create_time = None
            source = fp[engine].source_revision
        return WorkerHealth(
            state=state,  # type: ignore[arg-type]
            pid=pid,
            create_time=create_time,
            python_executable=self._engine_python(engine),
            python_version=self._python_version,
            source_revision=source,
            fingerprint=fp[engine],
            preflight_ok=True,
            active_inference=active,
        )

    def _engine_python(self, engine: WorkerName) -> Path:
        identity = self._processes.engine_identity(engine)
        if identity is not None:
            return cast(Path, identity.python_executable)
        return Path(sys.executable)

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> TrackerLease:
        self._require_known(engine)
        return await self._tracker.begin(engine, job_id=job_id)

    # ------------------------------------------------------------------ #
    # PID registry (runtime/run/processes.json)
    # ------------------------------------------------------------------ #

    def _write_registry(self) -> None:
        if self._registry_path is None:
            return
        payload: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": self._instance_id,
            "audit_log": str(self._audit_log) if self._audit_log else None,
            "control": {
                "pid": self._control_pid if self._control_pid else os.getpid(),
                "create_time": self._control_create_time if self._control_create_time else 0.0,
            },
            "engine_lifecycle": self._engine_lifecycle,
            "workers": {
                engine: self._worker_health(engine).model_dump(mode="json") for engine in _ENGINES
            },
            "updated_at_utc": self._registry_timestamp(),
        }
        target = self._registry_path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".processes.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(target))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _registry_timestamp() -> str:
        from datetime import datetime

        return datetime.now(UTC).isoformat()

    @staticmethod
    def _require_known(engine: WorkerName) -> None:
        if engine not in ("indextts", "gpt_sovits"):
            raise ValueError(f"unknown engine: {engine}")
