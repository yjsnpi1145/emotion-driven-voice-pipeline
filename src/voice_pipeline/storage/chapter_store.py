from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.models.chapter import (
    ChapterRunRecord,
    ChapterSegmentProgress,
    ChapterSynthesisRequest,
    ChapterTimeline,
)
from voice_pipeline.models.persistence import (
    CreateDubbingTaskRequest,
    CreateSegmentRequest,
    JobStatus,
    SegmentRecord,
)
from voice_pipeline.models.schemas import AudioResult
from voice_pipeline.modules.llm.director import validate_director_plan
from voice_pipeline.modules.llm.models import DirectorPlan
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import chapter_run_segments, chapter_runs, generation_jobs, segments
from voice_pipeline.storage.segment_store import SegmentStore


class ChapterStore:
    """Durable chapter run records and their source-derived segment mapping."""

    def __init__(self, database: Database, segments: SegmentStore) -> None:
        self._database = database
        self._segments = segments

    async def create_queued(
        self,
        *,
        request: ChapterSynthesisRequest,
        director_plan: DirectorPlan,
        model_profile_snapshot: dict[str, object],
        base_voice_sha256: str,
    ) -> ChapterRunRecord:
        materialized = validate_director_plan(request.source_text, director_plan)
        task = await self._segments.create_task(
            CreateDubbingTaskRequest(
                title=request.title,
                source_text=request.source_text,
                target_language=request.target_language,
                output_spec=request.output_spec,
            )
        )
        created_segments = []
        for item in materialized:
            created_segments.append(
                await self._segments.create_segment(
                    task.task_id,
                    CreateSegmentRequest(
                        ordinal=item.ordinal,
                        source_start=item.source_start,
                        source_end=item.source_end,
                        source_text=item.source_text,
                        synthesis_text=item.synthesis_text,
                        llm_emotion_vector=item.emotion_vector,
                        ref_text_cn=item.ref_text_cn,
                        speed_factor=item.speed_factor,
                        pause_after_ms=item.pause_after_ms,
                        seed=item.seed,
                    ),
                )
            )
        run_id = uuid4()
        now = _now()
        snapshot = {
            "schema_version": 1,
            "request": request.model_dump(mode="json"),
            "base_voice_sha256": base_voice_sha256,
            "model_profile_snapshot": model_profile_snapshot,
        }
        async with self._database.write_session() as session:
            await session.execute(
                insert(chapter_runs).values(
                    run_id=str(run_id),
                    request_id=str(request.request_id),
                    task_id=str(task.task_id),
                    status="queued",
                    base_voice_sha256=base_voice_sha256,
                    snapshot_json=_canonical_json(snapshot),
                    director_plan_json=director_plan.model_dump_json(),
                    model_profile_snapshot_json=_canonical_json(model_profile_snapshot),
                    final_audio_json=None,
                    final_relative_path=None,
                    timeline_json=None,
                    error_json=None,
                    created_at_utc=now,
                    started_at_utc=None,
                    finished_at_utc=None,
                )
            )
            await session.execute(
                insert(chapter_run_segments),
                [
                    {
                        "run_id": str(run_id),
                        "ordinal": segment.ordinal,
                        "segment_id": str(segment.segment_id),
                    }
                    for segment in created_segments
                ],
            )
        return await self.get(run_id)

    async def get(self, run_id: UUID) -> ChapterRunRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(chapter_runs).where(chapter_runs.c.run_id == str(run_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown chapter run: {run_id}")
        return _record(dict(row))

    async def list_runs(self, *, limit: int = 100) -> list[ChapterRunRecord]:
        """Return recent chapter runs without exposing their private snapshots."""
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    select(chapter_runs)
                    .order_by(chapter_runs.c.created_at_utc.desc(), chapter_runs.c.run_id.desc())
                    .limit(limit)
                )
            ).mappings()
            return [_record(dict(row)) for row in rows]

    async def list_segments(self, run_id: UUID) -> list[SegmentRecord]:
        run = await self.get(run_id)
        return await self._segments.list_segments(run.task_id)

    async def set_segment_job(
        self,
        run_id: UUID,
        ordinal: int,
        kind: Literal["reference", "gsv"],
        job_id: UUID,
    ) -> None:
        """Persist a submitted job before any worker has a chance to complete it."""
        column = (
            chapter_run_segments.c.reference_job_id
            if kind == "reference"
            else chapter_run_segments.c.gsv_job_id
        )
        async with self._database.write_session() as session:
            result = await session.execute(
                update(chapter_run_segments)
                .where(chapter_run_segments.c.run_id == str(run_id))
                .where(chapter_run_segments.c.ordinal == ordinal)
                .values({column.key: str(job_id)})
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"unknown chapter segment: {run_id}/{ordinal}")

    async def progress(self, run_id: UUID) -> tuple[ChapterSegmentProgress, ...]:
        """Return ordered progress using only durable job and segment state."""
        await self.get(run_id)
        reference_jobs = generation_jobs.alias("reference_jobs")
        gsv_jobs = generation_jobs.alias("gsv_jobs")
        statement = (
            select(
                chapter_run_segments.c.ordinal,
                chapter_run_segments.c.segment_id,
                segments.c.source_text,
                segments.c.active_ref_version_id,
                segments.c.active_gsv_version_id,
                reference_jobs.c.status.label("reference_job_status"),
                gsv_jobs.c.status.label("gsv_job_status"),
            )
            .join(segments, segments.c.segment_id == chapter_run_segments.c.segment_id)
            .outerjoin(
                reference_jobs, reference_jobs.c.job_id == chapter_run_segments.c.reference_job_id
            )
            .outerjoin(gsv_jobs, gsv_jobs.c.job_id == chapter_run_segments.c.gsv_job_id)
            .where(chapter_run_segments.c.run_id == str(run_id))
            .order_by(chapter_run_segments.c.ordinal)
        )
        async with self._database.read_session() as session:
            rows = (await session.execute(statement)).mappings().all()
        return tuple(
            ChapterSegmentProgress(
                ordinal=int(row["ordinal"]),
                segment_id=UUID(str(row["segment_id"])),
                source_summary=_source_summary(str(row["source_text"])),
                reference_job_status=_job_status(row["reference_job_status"]),
                gsv_job_status=_job_status(row["gsv_job_status"]),
                active_ref_version_id=(
                    UUID(str(row["active_ref_version_id"]))
                    if row["active_ref_version_id"] is not None
                    else None
                ),
                active_gsv_version_id=(
                    UUID(str(row["active_gsv_version_id"]))
                    if row["active_gsv_version_id"] is not None
                    else None
                ),
            )
            for row in rows
        )

    async def mark_running(self, run_id: UUID) -> ChapterRunRecord:
        return await self._transition(run_id, from_status=("queued",), to_status="running")

    async def mark_failed(self, run_id: UUID, error: dict[str, object]) -> ChapterRunRecord:
        return await self._transition(
            run_id,
            from_status=("queued", "running"),
            to_status="failed",
            error=error,
        )

    async def mark_interrupted_running(self) -> tuple[UUID, ...]:
        async with self._database.write_session() as session:
            rows = (
                (
                    await session.execute(
                        select(chapter_runs.c.run_id).where(chapter_runs.c.status == "running")
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                await session.execute(
                    update(chapter_runs)
                    .where(chapter_runs.c.status == "running")
                    .values(status="interrupted", finished_at_utc=_now())
                )
        return tuple(UUID(str(value)) for value in rows)

    async def mark_succeeded(
        self,
        run_id: UUID,
        *,
        final_audio: AudioResult,
        final_relative_path: str,
        timeline: ChapterTimeline,
    ) -> ChapterRunRecord:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(chapter_runs)
                .where(chapter_runs.c.run_id == str(run_id))
                .where(chapter_runs.c.status == "running")
                .values(
                    status="succeeded",
                    final_audio_json=final_audio.model_dump_json(),
                    final_relative_path=final_relative_path,
                    timeline_json=timeline.model_dump_json(),
                    finished_at_utc=_now(),
                )
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"chapter run is not running: {run_id}")
        return await self.get(run_id)

    async def _transition(
        self,
        run_id: UUID,
        *,
        from_status: tuple[str, ...],
        to_status: str,
        error: dict[str, object] | None = None,
    ) -> ChapterRunRecord:
        values: dict[str, object] = {"status": to_status}
        if to_status == "running":
            values["started_at_utc"] = _now()
        if to_status in {"failed", "cancelled", "interrupted"}:
            values["finished_at_utc"] = _now()
        if error is not None:
            values["error_json"] = _canonical_json(error)
        async with self._database.write_session() as session:
            result = await session.execute(
                update(chapter_runs)
                .where(chapter_runs.c.run_id == str(run_id))
                .where(chapter_runs.c.status.in_(from_status))
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"chapter run has unexpected state: {run_id}")
        return await self.get(run_id)


def _record(row: dict[str, Any]) -> ChapterRunRecord:
    final_audio = row["final_audio_json"]
    timeline = row["timeline_json"]
    error = row["error_json"]
    return ChapterRunRecord(
        run_id=UUID(str(row["run_id"])),
        request_id=UUID(str(row["request_id"])),
        task_id=UUID(str(row["task_id"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        snapshot=json.loads(str(row["snapshot_json"])),
        director_plan=json.loads(str(row["director_plan_json"])),
        base_voice_sha256=str(row["base_voice_sha256"]),
        final_audio=AudioResult.model_validate_json(str(final_audio))
        if final_audio is not None
        else None,
        final_relative_path=(
            str(row["final_relative_path"]) if row["final_relative_path"] else None
        ),
        timeline=ChapterTimeline.model_validate_json(str(timeline))
        if timeline is not None
        else None,
        error=json.loads(str(error)) if error is not None else None,
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        started_at_utc=(
            datetime.fromisoformat(str(row["started_at_utc"]))
            if row["started_at_utc"] is not None
            else None
        ),
        finished_at_utc=(
            datetime.fromisoformat(str(row["finished_at_utc"]))
            if row["finished_at_utc"] is not None
            else None
        ),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _source_summary(source_text: str) -> str:
    normalized = " ".join(source_text.split())
    return normalized if len(normalized) <= 120 else f"{normalized[:117]}..."


def _job_status(value: object) -> JobStatus | None:
    return cast(JobStatus, str(value)) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
