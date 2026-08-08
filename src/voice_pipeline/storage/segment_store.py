from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import (
    CreateDubbingTaskRequest,
    CreateSegmentRequest,
    DubbingTaskRecord,
    SegmentInputsPatch,
    SegmentRecord,
)
from voice_pipeline.models.schemas import EmotionVector
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import dubbing_tasks, segments


class SegmentStore:
    """SQLite source of truth for low-level tasks and editable segment drafts."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_task(self, request: CreateDubbingTaskRequest) -> DubbingTaskRecord:
        task_id = uuid4()
        now = _now()
        async with self._database.write_session() as session:
            await session.execute(
                insert(dubbing_tasks).values(
                    task_id=str(task_id),
                    title=request.title,
                    source_text=request.source_text,
                    source_text_sha256=_text_sha256(request.source_text),
                    target_language=request.target_language,
                    output_spec_json=request.output_spec.model_dump_json(),
                    revision=0,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        return await self.get_task(task_id)

    async def get_task(self, task_id: UUID) -> DubbingTaskRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(dubbing_tasks).where(dubbing_tasks.c.task_id == str(task_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return _task(dict(row))

    async def create_segment(self, task_id: UUID, request: CreateSegmentRequest) -> SegmentRecord:
        segment_id = uuid4()
        now = _now()
        async with self._database.write_session() as session:
            task_row = (
                (
                    await session.execute(
                        select(dubbing_tasks.c.source_text, dubbing_tasks.c.target_language).where(
                            dubbing_tasks.c.task_id == str(task_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if task_row is None:
                raise KeyError(f"unknown task: {task_id}")
            authoritative_slice = str(task_row["source_text"])[
                request.source_start : request.source_end
            ]
            if authoritative_slice != request.source_text:
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "segments",
                    "segment source_text must exactly match the task source slice",
                    retryable=False,
                )
            vector = list(request.llm_emotion_vector)
            try:
                await session.execute(
                    insert(segments).values(
                        segment_id=str(segment_id),
                        task_id=str(task_id),
                        ordinal=request.ordinal,
                        source_start=request.source_start,
                        source_end=request.source_end,
                        source_text=request.source_text,
                        synthesis_text=request.synthesis_text,
                        target_language=task_row["target_language"],
                        llm_emotion_vector_json=_canonical_json(vector),
                        current_emotion_vector_json=_canonical_json(vector),
                        ref_text_cn=request.ref_text_cn,
                        speed_factor=request.speed_factor,
                        pause_after_ms=request.pause_after_ms,
                        seed=request.seed,
                        ref_draft_revision=0,
                        gsv_draft_revision=0,
                        selection_revision=0,
                        active_ref_version_id=None,
                        active_gsv_version_id=None,
                        revision=0,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
            except IntegrityError as exc:
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "segments",
                    "segment ordinal already exists in this task",
                    retryable=False,
                ) from exc
        return await self.get_segment(segment_id)

    async def get_segment(self, segment_id: UUID) -> SegmentRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(segments).where(segments.c.segment_id == str(segment_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown segment: {segment_id}")
        return _segment(dict(row))

    async def list_segments(self, task_id: UUID) -> list[SegmentRecord]:
        await self.get_task(task_id)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(segments)
                        .where(segments.c.task_id == str(task_id))
                        .order_by(segments.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        return [_segment(dict(row)) for row in rows]

    async def patch_inputs(self, segment_id: UUID, patch: SegmentInputsPatch) -> SegmentRecord:
        reference_changed = any(
            value is not None
            for value in (patch.ref_text_cn, patch.current_emotion_vector, patch.seed)
        )
        gsv_changed = any(
            value is not None for value in (patch.synthesis_text, patch.speed_factor, patch.seed)
        )
        values: dict[str, Any] = {
            "ref_draft_revision": segments.c.ref_draft_revision + int(reference_changed),
            "gsv_draft_revision": segments.c.gsv_draft_revision + int(gsv_changed),
            "revision": segments.c.revision + 1,
            "updated_at_utc": _now(),
        }
        if patch.ref_text_cn is not None:
            values["ref_text_cn"] = patch.ref_text_cn
        if patch.current_emotion_vector is not None:
            values["current_emotion_vector_json"] = _canonical_json(
                list(patch.current_emotion_vector)
            )
        if patch.synthesis_text is not None:
            values["synthesis_text"] = patch.synthesis_text
        if patch.speed_factor is not None:
            values["speed_factor"] = patch.speed_factor
        if patch.pause_after_ms is not None:
            values["pause_after_ms"] = patch.pause_after_ms
        if patch.seed is not None:
            values["seed"] = patch.seed
        async with self._database.write_session() as session:
            result = await session.execute(
                update(segments)
                .where(segments.c.segment_id == str(segment_id))
                .where(segments.c.ref_draft_revision == patch.expected_ref_draft_revision)
                .where(segments.c.gsv_draft_revision == patch.expected_gsv_draft_revision)
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                exists = (
                    await session.execute(
                        select(segments.c.segment_id).where(
                            segments.c.segment_id == str(segment_id)
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise KeyError(f"unknown segment: {segment_id}")
                raise PipelineError(
                    ErrorCode.VERSION_CONFLICT,
                    "segments",
                    "segment draft revisions have changed",
                    retryable=False,
                )
        return await self.get_segment(segment_id)


def _task(row: dict[str, Any]) -> DubbingTaskRecord:
    from voice_pipeline.models.persistence import OutputAudioSpec

    return DubbingTaskRecord(
        task_id=UUID(str(row["task_id"])),
        title=str(row["title"]),
        source_text=str(row["source_text"]),
        target_language=str(row["target_language"]),  # type: ignore[arg-type]
        output_spec=OutputAudioSpec.model_validate_json(str(row["output_spec_json"])),
        revision=int(str(row["revision"])),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )


def _segment(row: dict[str, Any]) -> SegmentRecord:
    return SegmentRecord(
        segment_id=UUID(str(row["segment_id"])),
        task_id=UUID(str(row["task_id"])),
        ordinal=int(str(row["ordinal"])),
        source_start=int(str(row["source_start"])),
        source_end=int(str(row["source_end"])),
        source_text=str(row["source_text"]),
        synthesis_text=str(row["synthesis_text"]),
        target_language=str(row["target_language"]),  # type: ignore[arg-type]
        llm_emotion_vector=cast(
            EmotionVector, tuple(json.loads(str(row["llm_emotion_vector_json"])))
        ),
        current_emotion_vector=cast(
            EmotionVector, tuple(json.loads(str(row["current_emotion_vector_json"])))
        ),
        ref_text_cn=str(row["ref_text_cn"]),
        speed_factor=float(str(row["speed_factor"])),
        pause_after_ms=int(str(row["pause_after_ms"])),
        seed=int(str(row["seed"])),
        ref_draft_revision=int(str(row["ref_draft_revision"])),
        gsv_draft_revision=int(str(row["gsv_draft_revision"])),
        selection_revision=int(str(row["selection_revision"])),
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
        revision=int(str(row["revision"])),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
