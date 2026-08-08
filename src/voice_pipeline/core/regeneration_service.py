from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.segment_job_service import SegmentJobService
from voice_pipeline.models.persistence import (
    PersistentJobRecord,
    SegmentGsvJobRequest,
    SegmentReferenceJobRequest,
)
from voice_pipeline.models.schemas import ExecutionContext
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.segment_store import SegmentStore
from voice_pipeline.storage.version_store import VersionStore


class SegmentRegenerationService:
    """Coordinates explicit one-segment commands through the durable dispatcher."""

    def __init__(
        self,
        *,
        jobs: SqliteJobStore,
        chapters: ChapterStore,
        segments: SegmentStore,
        versions: VersionStore,
        segment_jobs: SegmentJobService,
        notify_jobs: Callable[[], Awaitable[None]],
    ) -> None:
        self._jobs = jobs
        self._chapters = chapters
        self._segments = segments
        self._versions = versions
        self._segment_jobs = segment_jobs
        self._notify_jobs = notify_jobs
        self._active: dict[UUID, asyncio.Task[None]] = {}

    async def submit_reference(
        self, segment_id: UUID, request: SegmentReferenceJobRequest
    ) -> ExecutionContext:
        context = await self._segment_jobs.submit_reference(segment_id, request)
        await self._chapters.set_segment_job_by_segment(segment_id, "reference", context.job_id)
        await self._notify_jobs()
        return context

    async def submit_gsv(self, segment_id: UUID, request: SegmentGsvJobRequest) -> ExecutionContext:
        context = await self._segment_jobs.submit_gsv(segment_id, request)
        await self._chapters.set_segment_job_by_segment(segment_id, "gsv", context.job_id)
        await self._notify_jobs()
        return context

    async def submit_both(
        self,
        segment_id: UUID,
        *,
        request_id: UUID,
        base_voice_path: Path,
        model_profile_id: UUID | None,
    ) -> ExecutionContext:
        reference = await self.submit_reference(
            segment_id,
            SegmentReferenceJobRequest(request_id=request_id, base_voice_path=base_voice_path),
        )
        task = asyncio.create_task(
            self._finish_both(segment_id, reference.job_id, model_profile_id),
            name=f"regenerate-both-{reference.job_id}",
        )
        self._active[reference.job_id] = task
        task.add_done_callback(lambda _: self._active.pop(reference.job_id, None))
        return reference

    async def stop(self, *, deadline: float) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            timeout = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait(tasks, timeout=timeout)

    async def _finish_both(
        self, segment_id: UUID, reference_job_id: UUID, model_profile_id: UUID | None
    ) -> None:
        reference = await self._await_job(reference_job_id)
        reference_version_id = await self._version_for_job(reference_job_id)
        segment = await self._segments.get_segment(segment_id)
        if segment.active_ref_version_id != reference_version_id:
            return
        await self.submit_gsv(
            segment_id,
            SegmentGsvJobRequest(request_id=uuid4(), model_profile_id=model_profile_id),
        )
        if reference.status != "succeeded":  # pragma: no cover - _await_job raises otherwise
            raise AssertionError("reference job did not succeed")

    async def _await_job(self, job_id: UUID) -> PersistentJobRecord:
        while True:
            record = await self._jobs.get(job_id)
            if record.status == "succeeded":
                return record
            if record.status in {"failed", "cancelled", "interrupted"}:
                error = record.error or {}
                code = _error_code(error)
                raise PipelineError(
                    code,
                    "regeneration",
                    str(error.get("message", "segment generation did not succeed")),
                    retryable=bool(error.get("retryable", False)),
                    details={"job_id": str(job_id), "status": record.status},
                )
            await asyncio.sleep(0.02)

    async def _version_for_job(self, job_id: UUID) -> UUID:
        record = await self._jobs.get(job_id)
        if record.task_snapshot is None:
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "regeneration",
                "reference job lacks a segment snapshot",
                retryable=False,
            )
        versions = await self._versions.list_versions(record.task_snapshot.segment_id)
        for version in versions:
            if version.source_job_id == job_id:
                return version.version_id
        raise PipelineError(
            ErrorCode.DATABASE_INTEGRITY_FAILED,
            "regeneration",
            "succeeded reference job has no immutable version",
            retryable=False,
        )


def _error_code(error: Mapping[str, object]) -> ErrorCode:
    try:
        return ErrorCode(str(error.get("code", ErrorCode.ENGINE_UNAVAILABLE.value)))
    except ValueError:
        return ErrorCode.ENGINE_UNAVAILABLE
