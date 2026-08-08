from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import (
    JobKind,
    JobStatus,
    JsonValue,
    PersistentJobRecord,
    RecoverySummary,
    SegmentJobSnapshot,
)
from voice_pipeline.models.schemas import ExecutionContext
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import generation_jobs


class SqliteJobStore:
    def __init__(self, database: Database, *, jobs_root: Path) -> None:
        self._database = database
        self._jobs_root = jobs_root.resolve()

    @staticmethod
    def canonical_json(payload: dict[str, JsonValue]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def canonical_sha(cls, payload: dict[str, JsonValue]) -> str:
        return hashlib.sha256(cls.canonical_json(payload).encode("utf-8")).hexdigest()

    async def create(
        self,
        *,
        request_id: UUID,
        kind: JobKind,
        request_snapshot: dict[str, JsonValue],
        segment_snapshot: SegmentJobSnapshot | None = None,
        retry_of_job_id: UUID | None = None,
        attempt: int = 1,
    ) -> ExecutionContext:
        snapshot_json = self.canonical_json(request_snapshot)
        job_id = uuid4()
        now = _utc_now()
        async with self._database.write_session() as session:
            await session.execute(
                insert(generation_jobs).values(
                    job_id=str(job_id),
                    request_id=str(request_id),
                    kind=kind,
                    status="queued",
                    stage="queued",
                    task_id=(
                        str(segment_snapshot.task_id) if segment_snapshot is not None else None
                    ),
                    segment_id=(
                        str(segment_snapshot.segment_id) if segment_snapshot is not None else None
                    ),
                    retry_of_job_id=str(retry_of_job_id) if retry_of_job_id is not None else None,
                    attempt=attempt,
                    request_snapshot_json=snapshot_json,
                    request_snapshot_sha256=hashlib.sha256(
                        snapshot_json.encode("utf-8")
                    ).hexdigest(),
                    model_fingerprint_json="{}",
                    model_profile_snapshot_json=None,
                    output_spec_json=None,
                    segment_snapshot_json=(
                        segment_snapshot.model_dump_json() if segment_snapshot is not None else None
                    ),
                    cancel_requested_at_utc=None,
                    runner_instance_id=None,
                    result_json=None,
                    error_json=None,
                    activation_outcome="not_applicable",
                    created_at_utc=now,
                    started_at_utc=None,
                    finished_at_utc=None,
                )
            )
        return ExecutionContext(
            job_id=job_id, request_id=request_id, job_dir=self._jobs_root / str(job_id)
        )

    async def get(self, job_id: UUID) -> PersistentJobRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(generation_jobs).where(generation_jobs.c.job_id == str(job_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        return _record(dict(row))

    async def list_queued(self, *, limit: int) -> list[PersistentJobRecord]:
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    select(generation_jobs)
                    .where(generation_jobs.c.status == "queued")
                    .order_by(generation_jobs.c.created_at_utc, generation_jobs.c.job_id)
                    .limit(limit)
                )
            ).mappings()
            return [_record(dict(row)) for row in rows]

    async def mark_running(self, job_id: UUID) -> bool:
        return await self._transition_running(job_id, instance_id=None)

    async def claim(self, job_id: UUID, *, instance_id: UUID) -> bool:
        return await self._transition_running(job_id, instance_id=instance_id)

    async def _transition_running(self, job_id: UUID, *, instance_id: UUID | None) -> bool:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(generation_jobs)
                .where(generation_jobs.c.job_id == str(job_id))
                .where(generation_jobs.c.status == "queued")
                .where(generation_jobs.c.cancel_requested_at_utc.is_(None))
                .values(
                    status="running",
                    stage="running",
                    runner_instance_id=str(instance_id) if instance_id is not None else None,
                    started_at_utc=_utc_now(),
                )
            )
        return cast(CursorResult[Any], result).rowcount == 1

    async def mark_succeeded(self, job_id: UUID, *, result: dict[str, JsonValue]) -> bool:
        return await self._mark_terminal(job_id, status="succeeded", result=result, error=None)

    async def mark_failed(self, job_id: UUID, *, error: dict[str, JsonValue]) -> bool:
        # A queue timeout is raised before the worker factory can claim the job.
        # It is still a terminal execution outcome and must not strand the durable
        # record in ``queued`` after the scheduler has returned.
        return await self._mark_terminal(
            job_id,
            status="failed",
            result=None,
            error=error,
            allowed_previous_statuses=("queued", "running"),
        )

    async def mark_cancelled(self, job_id: UUID, *, error: dict[str, JsonValue]) -> bool:
        return await self._mark_terminal(
            job_id,
            status="cancelled",
            result=None,
            error=error,
            allowed_previous_statuses=("queued", "running"),
        )

    async def fail_unfinished(self, *, error: dict[str, JsonValue]) -> int:
        """Fail queued/running jobs during a coordinated control-plane shutdown.

        Queue cancellation wakes individual schedulers asynchronously.  Recording
        the terminal state here keeps the HTTP shutdown receipt truthful before
        it is returned, while the per-job compare-and-set prevents a late worker
        from overwriting it.
        """
        async with self._database.write_session() as session:
            result = await session.execute(
                update(generation_jobs)
                .where(generation_jobs.c.status.in_(("queued", "running")))
                .values(
                    status="failed",
                    stage="failed",
                    error_json=self.canonical_json(error),
                    finished_at_utc=_utc_now(),
                )
            )
        return int(cast(CursorResult[Any], result).rowcount)

    async def cancel(self, job_id: UUID) -> PersistentJobRecord:
        """Linearize a cancellation request against the durable job state.

        A queued job becomes terminal immediately.  A running job only receives
        the durable cancellation marker here; its dispatcher owns engine abort
        and commits ``cancelled`` after active inference reaches zero.
        """
        now = _utc_now()
        async with self._database.write_session() as session:
            queued = await session.execute(
                update(generation_jobs)
                .where(generation_jobs.c.job_id == str(job_id))
                .where(generation_jobs.c.status == "queued")
                .where(generation_jobs.c.cancel_requested_at_utc.is_(None))
                .values(
                    status="cancelled",
                    stage="cancelled",
                    cancel_requested_at_utc=now,
                    finished_at_utc=now,
                    error_json=self.canonical_json(_cancel_error("queued")),
                )
            )
            if cast(CursorResult[Any], queued).rowcount == 0:
                running = await session.execute(
                    update(generation_jobs)
                    .where(generation_jobs.c.job_id == str(job_id))
                    .where(generation_jobs.c.status == "running")
                    .where(generation_jobs.c.cancel_requested_at_utc.is_(None))
                    .values(cancel_requested_at_utc=now)
                )
                if cast(CursorResult[Any], running).rowcount == 0:
                    row = (
                        (
                            await session.execute(
                                select(generation_jobs).where(
                                    generation_jobs.c.job_id == str(job_id)
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is None:
                        raise KeyError(f"unknown job: {job_id}")
                    record = _record(dict(row))
                    if record.status == "cancelled":
                        return record
                    raise PipelineError(
                        ErrorCode.JOB_STATE_CONFLICT,
                        "jobs",
                        f"job is already {record.status}",
                        retryable=False,
                        details={"status": record.status},
                    )
        return await self.get(job_id)

    async def clone_for_retry(self, job_id: UUID) -> ExecutionContext:
        """Create a new queued execution from an immutable terminal snapshot."""
        async with self._database.write_session() as session:
            row = (
                (
                    await session.execute(
                        select(generation_jobs).where(generation_jobs.c.job_id == str(job_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise KeyError(f"unknown job: {job_id}")
            original = dict(row)
            status = str(original["status"])
            if status not in ("failed", "cancelled", "interrupted"):
                raise PipelineError(
                    ErrorCode.JOB_NOT_RETRYABLE,
                    "jobs",
                    f"job in {status} state cannot be retried",
                    retryable=False,
                    details={"status": status},
                )
            new_job_id = uuid4()
            await session.execute(
                insert(generation_jobs).values(
                    job_id=str(new_job_id),
                    request_id=original["request_id"],
                    kind=original["kind"],
                    status="queued",
                    stage="queued",
                    task_id=original["task_id"],
                    segment_id=original["segment_id"],
                    retry_of_job_id=str(job_id),
                    attempt=int(str(original["attempt"])) + 1,
                    request_snapshot_json=original["request_snapshot_json"],
                    request_snapshot_sha256=original["request_snapshot_sha256"],
                    model_fingerprint_json=original["model_fingerprint_json"],
                    model_profile_snapshot_json=original["model_profile_snapshot_json"],
                    output_spec_json=original["output_spec_json"],
                    segment_snapshot_json=original["segment_snapshot_json"],
                    cancel_requested_at_utc=None,
                    runner_instance_id=None,
                    result_json=None,
                    error_json=None,
                    activation_outcome="not_applicable",
                    created_at_utc=_utc_now(),
                    started_at_utc=None,
                    finished_at_utc=None,
                )
            )
        return ExecutionContext(
            job_id=new_job_id,
            request_id=UUID(str(original["request_id"])),
            job_dir=self._jobs_root / str(new_job_id),
        )

    async def recover_interrupted(self) -> RecoverySummary:
        """Mark only an abandoned previous-instance run as interrupted."""
        recovery_error = self.canonical_json(
            {
                "code": ErrorCode.ENGINE_UNAVAILABLE.value,
                "stage": "recovery",
                "message": "control process restarted while this job was running",
                "retryable": True,
                "details": {"reason": "process_restart"},
            }
        )
        async with self._database.write_session() as session:
            running_rows = (
                (
                    await session.execute(
                        select(generation_jobs.c.job_id)
                        .where(generation_jobs.c.status == "running")
                        .order_by(generation_jobs.c.created_at_utc, generation_jobs.c.job_id)
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                update(generation_jobs)
                .where(generation_jobs.c.status == "running")
                .values(
                    status="interrupted",
                    stage="interrupted",
                    error_json=recovery_error,
                    finished_at_utc=_utc_now(),
                )
            )
            queued_rows = (
                (
                    await session.execute(
                        select(generation_jobs.c.job_id)
                        .where(generation_jobs.c.status == "queued")
                        .order_by(generation_jobs.c.created_at_utc, generation_jobs.c.job_id)
                    )
                )
                .scalars()
                .all()
            )
        return RecoverySummary(
            interrupted_job_ids=tuple(UUID(str(value)) for value in running_rows),
            queued_job_ids=tuple(UUID(str(value)) for value in queued_rows),
        )

    async def _mark_terminal(
        self,
        job_id: UUID,
        *,
        status: JobStatus,
        result: dict[str, JsonValue] | None,
        error: dict[str, JsonValue] | None,
        allowed_previous_statuses: tuple[JobStatus, ...] = ("running",),
    ) -> bool:
        if status not in ("succeeded", "failed", "cancelled"):
            raise ValueError(f"invalid terminal status: {status}")
        async with self._database.write_session() as session:
            statement = (
                update(generation_jobs)
                .where(generation_jobs.c.job_id == str(job_id))
                .where(generation_jobs.c.status.in_(allowed_previous_statuses))
            )
            if status == "succeeded":
                statement = statement.where(generation_jobs.c.cancel_requested_at_utc.is_(None))
            update_result = await session.execute(
                statement.values(
                    status=status,
                    stage=status,
                    result_json=(self.canonical_json(result) if result is not None else None),
                    error_json=(self.canonical_json(error) if error is not None else None),
                    finished_at_utc=_utc_now(),
                )
            )
        return cast(CursorResult[Any], update_result).rowcount == 1


def _record(row: dict[str, Any]) -> PersistentJobRecord:
    return PersistentJobRecord(
        job_id=UUID(str(row["job_id"])),
        request_id=UUID(str(row["request_id"])),
        kind=cast(JobKind, str(row["kind"])),
        status=cast(JobStatus, str(row["status"])),
        stage=str(row["stage"]),
        request_snapshot=cast(dict[str, JsonValue], json.loads(str(row["request_snapshot_json"]))),
        request_snapshot_sha256=str(row["request_snapshot_sha256"]),
        model_fingerprint=cast(
            dict[str, JsonValue], json.loads(str(row["model_fingerprint_json"]))
        ),
        retry_of_job_id=(UUID(str(row["retry_of_job_id"])) if row["retry_of_job_id"] else None),
        attempt=int(str(row["attempt"])),
        cancel_requested_at_utc=_parse_time(row["cancel_requested_at_utc"]),
        result=cast(dict[str, JsonValue] | None, _parse_json(row["result_json"])),
        error=cast(dict[str, JsonValue] | None, _parse_json(row["error_json"])),
        activation_outcome=cast(Any, str(row["activation_outcome"])),
        created_at_utc=_parse_time_required(row["created_at_utc"]),
        started_at_utc=_parse_time(row["started_at_utc"]),
        finished_at_utc=_parse_time(row["finished_at_utc"]),
    )


def _parse_json(value: object) -> object | None:
    return json.loads(str(value)) if value is not None else None


def _parse_time(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _parse_time_required(value: object) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:  # pragma: no cover - database NOT NULL invariant
        raise PipelineError(
            ErrorCode.DATABASE_INTEGRITY_FAILED,
            "storage",
            "stored job lacks creation timestamp",
            retryable=False,
        )
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _cancel_error(state: str) -> dict[str, JsonValue]:
    return {
        "code": "JOB_CANCELLED",
        "stage": "jobs",
        "message": f"job was cancelled while {state}",
        "retryable": False,
        "details": {"state": state},
    }
