from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict

from voice_pipeline.models.schemas import ExecutionContext, StrictModel

JobKind = Literal["reference", "gsv", "segment"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class JobRecord(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: UUID
    request_id: UUID
    kind: JobKind
    status: JobStatus
    stage: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    request_snapshot: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class InMemoryJobRegistry:
    def __init__(self, jobs_root: Path) -> None:
        self._jobs_root = jobs_root.resolve()
        self._lock = asyncio.Lock()
        self._records: dict[UUID, JobRecord] = {}

    async def create(
        self,
        *,
        request_id: UUID,
        kind: JobKind,
        request_snapshot: dict[str, Any],
    ) -> ExecutionContext:
        async with self._lock:
            job_id = uuid4()
            record = JobRecord(
                job_id=job_id,
                request_id=request_id,
                kind=kind,
                status="queued",
                stage="queued",
                created_at=datetime.now(UTC),
                request_snapshot=request_snapshot,
            )
            self._records[job_id] = record
            return ExecutionContext(
                job_id=job_id,
                request_id=request_id,
                job_dir=self._jobs_root / str(job_id),
            )

    async def get(self, job_id: UUID) -> JobRecord:
        async with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise KeyError(f"unknown job: {job_id}")
            return record.model_copy(deep=True)

    async def mark_running(self, job_id: UUID) -> None:
        await self._update(
            job_id,
            status="running",
            stage="running",
            started_at=datetime.now(UTC),
        )

    async def mark_succeeded(self, job_id: UUID, *, result: dict[str, Any]) -> None:
        await self._update(
            job_id,
            status="succeeded",
            stage="succeeded",
            finished_at=datetime.now(UTC),
            result=result,
        )

    async def mark_failed(self, job_id: UUID, *, error: dict[str, Any]) -> None:
        await self._update(
            job_id,
            status="failed",
            stage="failed",
            finished_at=datetime.now(UTC),
            error=error,
        )

    async def _update(self, job_id: UUID, **changes: Any) -> None:
        async with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise KeyError(f"unknown job: {job_id}")
            self._records[job_id] = record.model_copy(update=changes)
