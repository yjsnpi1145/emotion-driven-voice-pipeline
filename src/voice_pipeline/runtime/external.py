from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

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


class ExternalEngineRuntime:
    """Runtime for ``external_test`` mode.

    Does not own or start the fake engine processes; it only records health
    and abort callbacks against the external fake servers.
    """

    def __init__(
        self,
        *,
        settings: Any,
        fingerprints: dict[WorkerName, EngineFingerprint],
    ) -> None:
        self._settings = settings
        self._fingerprints = fingerprints
        self._tracker = InferenceTracker()
        self._state: dict[WorkerName, str] = {
            "indextts": "stopped_expected",
            "gpt_sovits": "stopped_expected",
        }

    async def start(self) -> None:
        return None

    async def stop(self, *, deadline: float | None = None) -> None:
        return None

    def _base_url(self, engine: WorkerName) -> str:
        if engine == "indextts":
            base_url: str = self._settings.engines.indextts.base_url
        else:
            base_url = self._settings.engines.gpt_sovits.base_url
        return base_url

    async def _fetch_health(self, engine: WorkerName) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base_url(engine), timeout=5.0) as client:
            resp = await client.get("/health/ready")
            if resp.status_code != 200:
                raise PipelineError(
                    ErrorCode.ENGINE_UNAVAILABLE,
                    "runtime",
                    f"{engine} health not ready",
                    retryable=True,
                )
            body: dict[str, Any] = resp.json()
            return body

    async def ensure_engine(self, engine: WorkerName) -> None:
        self._require_known(engine)
        health = await self._fetch_health(engine)
        fingerprint = health.get("fingerprint")
        expected = self._fingerprints[engine].model_dump(mode="json")
        if fingerprint != expected:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} fingerprint mismatch",
                retryable=False,
            )
        if health.get("state") != "ready":
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} not ready",
                retryable=False,
            )
        self._state[engine] = "ready"

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None:
        self._require_known(engine)
        try:
            async with httpx.AsyncClient(base_url=self._base_url(engine), timeout=10.0) as client:
                resp = await client.post("/__control/abort")
                body = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} abort could not be confirmed",
                retryable=False,
                poison_queue=True,
            ) from exc
        if resp.status_code != 200 or body.get("active_inference") != 0:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} abort did not confirm zero active inference",
                retryable=False,
                poison_queue=True,
            )
        self._state[engine] = "stopped_expected"

    def engine_identity(self, engine: WorkerName) -> EngineIdentity:
        self._require_known(engine)
        if self._state[engine] != "ready" or self._tracker.is_unknown(engine):
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} is not ready",
                retryable=False,
            )
        import os
        import sys

        return EngineIdentity(
            worker=engine,
            pid=os.getpid(),
            create_time=0.0,
            python_executable=Path(sys.executable),
            fingerprint=self._fingerprints[engine],
        )

    def health(self) -> RuntimeHealth:
        workers: dict[str, WorkerHealth] = {}
        for engine in ("indextts", "gpt_sovits"):
            state = "unknown" if self._tracker.is_unknown(engine) else self._state[engine]
            workers[engine] = WorkerHealth(
                state=state,  # type: ignore[arg-type]
                pid=None,
                create_time=None,
                python_executable=Path("python.exe"),
                python_version="3.11",
                source_revision=self._fingerprints[engine].source_revision,
                fingerprint=self._fingerprints[engine],
                preflight_ok=True,
                active_inference=self._tracker.active_count(engine),
            )
        states = [workers["indextts"].state, workers["gpt_sovits"].state]
        degraded = any(state in ("unknown", "unhealthy") for state in states)
        return RuntimeHealth(
            status="degraded" if degraded else "ready",
            workers=WorkersHealth(indextts=workers["indextts"], gpt_sovits=workers["gpt_sovits"]),
        )

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> TrackerLease:
        self._require_known(engine)
        return await self._tracker.begin(engine, job_id=job_id)

    @staticmethod
    def _require_known(engine: WorkerName) -> None:
        if engine not in ("indextts", "gpt_sovits"):
            raise ValueError(f"unknown engine: {engine}")
