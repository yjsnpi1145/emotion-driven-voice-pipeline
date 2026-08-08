from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from voice_pipeline.models.model_profiles import ResolvedModelProfile
from voice_pipeline.models.schemas import (
    AudioResult,
    EngineFingerprint,
    EngineIdentity,
    GsvSynthesisRequest,
    IndexSynthesisRequest,
    RuntimeHealth,
    WorkerName,
)


class IndexTTSClient(Protocol):
    async def synthesize(
        self, request: IndexSynthesisRequest, output_path: Path
    ) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


class GptSoVitsClient(Protocol):
    async def load_profile(self, profile: ResolvedModelProfile) -> None: ...

    async def synthesize(self, request: GsvSynthesisRequest, output_path: Path) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


class InferenceLease(Protocol):
    async def confirm_completed(self) -> None: ...
    async def confirm_aborted(self) -> None: ...
    async def mark_unknown(self) -> None: ...


class EngineRuntime(Protocol):
    async def start(self) -> None: ...

    async def stop(self, *, deadline: float | None = None) -> None: ...

    async def ensure_engine(self, engine: WorkerName) -> None: ...

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None: ...

    def engine_identity(self, engine: WorkerName) -> EngineIdentity: ...

    def health(self) -> RuntimeHealth: ...

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> InferenceLease: ...
