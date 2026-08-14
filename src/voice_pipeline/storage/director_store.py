from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateDirectorProjectRequest,
    CreateDirectorRole,
    CreateDirectorUtterance,
    DirectorProjectRecord,
    DirectorRoleRecord,
    DirectorUtteranceRecord,
)
from voice_pipeline.models.director_llm import (
    ChunkAnalysisResult,
    ScriptChunk,
    TranslationResultItem,
)
from voice_pipeline.models.schemas import EmotionVector
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import (
    director_analysis_chunks,
    director_edit_events,
    director_projects,
    director_roles,
    director_utterances,
    role_presets,
)


class DirectorStore:
    """SQLite source of truth for editable, revisioned director projects."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_project(self, request: CreateDirectorProjectRequest) -> DirectorProjectRecord:
        project_id = uuid4()
        now = _now()
        async with self._database.write_session() as session:
            await session.execute(
                insert(director_projects).values(
                    project_id=str(project_id),
                    title=request.title,
                    source_text=request.source_text,
                    source_text_sha256=_sha256(request.source_text),
                    source_language=request.source_language,
                    target_language=request.target_language,
                    narration_enabled=int(request.narration_enabled),
                    status="draft",
                    revision=0,
                    analysis_revision=0,
                    role_revision=0,
                    translation_revision=0,
                    mapping_revision=0,
                    generation_revision=0,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        return await self.get_project(project_id)

    async def get_project(self, project_id: UUID) -> DirectorProjectRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_projects)
                        .where(director_projects.c.project_id == str(project_id))
                        .where(director_projects.c.deleted_at_utc.is_(None))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown director project: {project_id}")
        return _project(dict(row))

    async def list_projects(self, *, limit: int = 100) -> list[DirectorProjectRecord]:
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_projects)
                        .where(director_projects.c.deleted_at_utc.is_(None))
                        .order_by(director_projects.c.updated_at_utc.desc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_project(dict(row)) for row in rows]

    async def begin_analysis(
        self, project_id: UUID, *, expected_revision: int
    ) -> DirectorProjectRecord:
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"draft", "analyzing", "role_review"},
            status="analyzing",
            event="analysis_started",
        )
        return await self.get_project(project_id)

    async def load_analysis_chunk(
        self, project_id: UUID, chunk: ScriptChunk
    ) -> ChunkAnalysisResult | None:
        chunk_id = _stored_chunk_id(project_id, chunk.chunk_id)
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(
                            director_analysis_chunks.c.source_sha256,
                            director_analysis_chunks.c.status,
                            director_analysis_chunks.c.result_json,
                        ).where(director_analysis_chunks.c.chunk_id == chunk_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            row is None
            or str(row["status"]) != "succeeded"
            or str(row["source_sha256"]) != _sha256(chunk.source_text)
            or row["result_json"] is None
        ):
            return None
        return ChunkAnalysisResult.model_validate_json(str(row["result_json"]))

    async def save_analysis_chunk(
        self,
        project_id: UUID,
        *,
        ordinal: int,
        chunk: ScriptChunk,
        result: ChunkAnalysisResult,
        llm_fingerprint: str,
    ) -> None:
        chunk_id = _stored_chunk_id(project_id, chunk.chunk_id)
        async with self._database.write_session() as session:
            existing = (
                await session.execute(
                    select(director_analysis_chunks.c.chunk_id).where(
                        director_analysis_chunks.c.chunk_id == chunk_id
                    )
                )
            ).scalar_one_or_none()
            values = {
                "project_id": str(project_id),
                "ordinal": ordinal,
                "source_start": chunk.source_start,
                "source_end": chunk.source_end,
                "source_sha256": _sha256(chunk.source_text),
                "llm_fingerprint": llm_fingerprint,
                "prompt_version": "director-analysis-v1",
                "schema_version": 1,
                "status": "succeeded",
                "result_json": result.model_dump_json(),
                "error_json": None,
                "updated_at_utc": _now(),
            }
            if existing is None:
                await session.execute(
                    insert(director_analysis_chunks).values(
                        chunk_id=chunk_id,
                        attempt=1,
                        **values,
                    )
                )
            else:
                await session.execute(
                    update(director_analysis_chunks)
                    .where(director_analysis_chunks.c.chunk_id == chunk_id)
                    .values(
                        attempt=director_analysis_chunks.c.attempt + 1,
                        **values,
                    )
                )

    async def publish_analysis(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        roles: Sequence[CreateDirectorRole],
        utterances: Sequence[CreateDirectorUtterance],
    ) -> DirectorProjectRecord:
        project = await self.get_project(project_id)
        validate_source_coverage(project.source_text, utterances)
        if not roles:
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "director",
                "analysis did not return any roles",
                retryable=False,
            )
        role_rows = [(uuid4(), role) for role in roles]
        role_by_name = {
            name.casefold(): role_id
            for role_id, role in role_rows
            for name in (role.canonical_name, *role.aliases)
        }
        narrator = next((item[0] for item in role_rows if item[1].kind == "narrator"), None)
        characters = [item[0] for item in role_rows if item[1].kind == "character"]
        async with self._database.write_session() as session:
            current = await _locked_project(session, project_id)
            _require_revision(current, expected_revision)
            if str(current["status"]) not in {"draft", "analyzing", "role_review"}:
                raise _state_conflict(str(current["status"]), "publish analysis")
            await session.execute(
                delete(director_utterances).where(
                    director_utterances.c.project_id == str(project_id)
                )
            )
            await session.execute(
                delete(director_roles).where(director_roles.c.project_id == str(project_id))
            )
            await session.execute(
                insert(director_roles),
                [
                    {
                        "role_id": str(role_id),
                        "project_id": str(project_id),
                        "canonical_name": role.canonical_name,
                        "kind": role.kind,
                        "aliases_json": _json(list(role.aliases)),
                        "confidence": role.confidence,
                        "preset_id": None,
                        "revision": 0,
                    }
                    for role_id, role in role_rows
                ],
            )
            materialized = []
            for item in utterances:
                role_id: UUID | None = None
                if item.kind == "narration":
                    role_id = narrator
                elif item.kind == "dialogue" and len(characters) == 1:
                    role_id = characters[0]
                if item.role_name:
                    role_id = role_by_name.get(item.role_name.casefold(), role_id)
                materialized.append(
                    {
                        "utterance_id": str(uuid4()),
                        "project_id": str(project_id),
                        "ordinal": item.ordinal,
                        "source_start": item.source_start,
                        "source_end": item.source_end,
                        "source_text": item.source_text,
                        "kind": item.kind,
                        "speak_enabled": int(
                            item.speak_enabled
                            and not (item.kind == "narration" and not project.narration_enabled)
                        ),
                        "role_id": str(item.role_id or role_id)
                        if (item.role_id or role_id)
                        else None,
                        "role_confidence": item.role_confidence,
                        "role_confirmed": int(item.role_confirmed),
                        "synthesis_text": None,
                        "ref_text_cn": None,
                        "emotion_vector_json": None,
                        "speed_factor": 1.0,
                        "pause_after_ms": 0,
                        "seed": 1234,
                        "revision": 0,
                    }
                )
            await session.execute(insert(director_utterances), materialized)
            new_revision = expected_revision + 1
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="role_review",
                    revision=new_revision,
                    analysis_revision=director_projects.c.analysis_revision + 1,
                    role_revision=director_projects.c.role_revision + 1,
                    translation_revision=0,
                    mapping_revision=0,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="analysis_published",
                before_revision=expected_revision,
                after_revision=new_revision,
                details={"roles": len(roles), "utterances": len(utterances)},
            )
        return await self.get_project(project_id)

    async def list_roles(self, project_id: UUID) -> list[DirectorRoleRecord]:
        await self.get_project(project_id)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_roles)
                        .where(director_roles.c.project_id == str(project_id))
                        .order_by(director_roles.c.kind, director_roles.c.canonical_name)
                    )
                )
                .mappings()
                .all()
            )
        return [_role(dict(row)) for row in rows]

    async def list_utterances(self, project_id: UUID) -> list[DirectorUtteranceRecord]:
        await self.get_project(project_id)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_utterances)
                        .where(director_utterances.c.project_id == str(project_id))
                        .order_by(director_utterances.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        return [_utterance(dict(row)) for row in rows]

    async def patch_utterance(
        self,
        utterance_id: UUID,
        *,
        expected_revision: int,
        **changes: object,
    ) -> DirectorUtteranceRecord:
        allowed = {
            "role_id",
            "speak_enabled",
            "role_confirmed",
            "synthesis_text",
            "ref_text_cn",
            "emotion_vector",
            "speed_factor",
            "pause_after_ms",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if "role_id" in values and values["role_id"] is not None:
            values["role_id"] = str(values["role_id"])
        if "emotion_vector" in values and values["emotion_vector"] is not None:
            values["emotion_vector_json"] = _json(
                list(cast(EmotionVector, values.pop("emotion_vector")))
            )
        for name in ("speak_enabled", "role_confirmed"):
            if name in values and values[name] is not None:
                values[name] = int(bool(values[name]))
        values = {key: value for key, value in values.items() if value is not None}
        values["revision"] = director_utterances.c.revision + 1
        async with self._database.write_session() as session:
            existing = (
                (
                    await session.execute(
                        select(director_utterances).where(
                            director_utterances.c.utterance_id == str(utterance_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise KeyError(f"unknown director utterance: {utterance_id}")
            result = await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .where(director_utterances.c.revision == expected_revision)
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise _version_conflict()
            project_id = UUID(str(existing["project_id"]))
            await _touch_project(session, project_id, role_change="role_id" in values)
            await _append_event(
                session,
                project_id,
                operation="utterance_patched",
                object_id=utterance_id,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"fields": sorted(values)},
            )
        return await self.get_utterance(utterance_id)

    async def get_utterance(self, utterance_id: UUID) -> DirectorUtteranceRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_utterances).where(
                            director_utterances.c.utterance_id == str(utterance_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown director utterance: {utterance_id}")
        return _utterance(dict(row))

    async def split_utterance(
        self,
        utterance_id: UUID,
        *,
        expected_revision: int,
        split_at: int,
    ) -> list[DirectorUtteranceRecord]:
        async with self._database.write_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_utterances).where(
                            director_utterances.c.utterance_id == str(utterance_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise KeyError(f"unknown director utterance: {utterance_id}")
            if int(row["revision"]) != expected_revision:
                raise _version_conflict()
            start, end = int(row["source_start"]), int(row["source_end"])
            if split_at <= start or split_at >= end:
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "split point is outside utterance",
                    retryable=False,
                )
            project_id = UUID(str(row["project_id"]))
            ordinal = int(row["ordinal"])
            later = (
                await session.execute(
                    select(director_utterances.c.utterance_id, director_utterances.c.ordinal)
                    .where(director_utterances.c.project_id == str(project_id))
                    .where(director_utterances.c.ordinal > ordinal)
                    .order_by(director_utterances.c.ordinal.desc())
                )
            ).all()
            for later_id, later_ordinal in later:
                await session.execute(
                    update(director_utterances)
                    .where(director_utterances.c.utterance_id == str(later_id))
                    .values(ordinal=int(later_ordinal) + 1)
                )
            source = str(row["source_text"])
            cut = split_at - start
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .values(
                    source_end=split_at,
                    source_text=source[:cut],
                    synthesis_text=None,
                    ref_text_cn=None,
                    emotion_vector_json=None,
                    role_confirmed=0,
                    revision=expected_revision + 1,
                )
            )
            right_id = uuid4()
            data = dict(row)
            data.update(
                utterance_id=str(right_id),
                ordinal=ordinal + 1,
                source_start=split_at,
                source_text=source[cut:],
                synthesis_text=None,
                ref_text_cn=None,
                emotion_vector_json=None,
                role_confirmed=0,
                revision=0,
            )
            await session.execute(insert(director_utterances).values(**data))
            await _touch_project(session, project_id, role_change=True)
            await _append_event(
                session,
                project_id,
                operation="utterance_split",
                object_id=utterance_id,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"right_utterance_id": str(right_id), "split_at": split_at},
            )
        return await self.list_utterances(project_id)

    async def merge_utterances(
        self,
        left_id: UUID,
        right_id: UUID,
        *,
        expected_left_revision: int,
        expected_right_revision: int,
    ) -> list[DirectorUtteranceRecord]:
        async with self._database.write_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_utterances).where(
                            director_utterances.c.utterance_id.in_([str(left_id), str(right_id)])
                        )
                    )
                )
                .mappings()
                .all()
            )
            by_id = {str(row["utterance_id"]): row for row in rows}
            if str(left_id) not in by_id or str(right_id) not in by_id:
                raise KeyError("unknown director utterance")
            left, right = by_id[str(left_id)], by_id[str(right_id)]
            if (
                int(left["revision"]) != expected_left_revision
                or int(right["revision"]) != expected_right_revision
            ):
                raise _version_conflict()
            if (
                left["project_id"] != right["project_id"]
                or int(right["ordinal"]) != int(left["ordinal"]) + 1
                or int(right["source_start"]) != int(left["source_end"])
            ):
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "only adjacent utterances can merge",
                    retryable=False,
                )
            project_id = UUID(str(left["project_id"]))
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(left_id))
                .values(
                    source_end=int(right["source_end"]),
                    source_text=str(left["source_text"]) + str(right["source_text"]),
                    synthesis_text=None,
                    ref_text_cn=None,
                    emotion_vector_json=None,
                    role_confirmed=0,
                    revision=expected_left_revision + 1,
                )
            )
            await session.execute(
                delete(director_utterances).where(
                    director_utterances.c.utterance_id == str(right_id)
                )
            )
            later = (
                await session.execute(
                    select(director_utterances.c.utterance_id, director_utterances.c.ordinal)
                    .where(director_utterances.c.project_id == str(project_id))
                    .where(director_utterances.c.ordinal > int(right["ordinal"]))
                    .order_by(director_utterances.c.ordinal)
                )
            ).all()
            for later_id, later_ordinal in later:
                await session.execute(
                    update(director_utterances)
                    .where(director_utterances.c.utterance_id == str(later_id))
                    .values(ordinal=int(later_ordinal) - 1)
                )
            await _touch_project(session, project_id, role_change=True)
            await _append_event(
                session,
                project_id,
                operation="utterances_merged",
                object_id=left_id,
                before_revision=expected_left_revision,
                after_revision=expected_left_revision + 1,
                details={"removed_utterance_id": str(right_id)},
            )
        return await self.list_utterances(project_id)

    async def set_narration_enabled(
        self, project_id: UUID, *, expected_revision: int, enabled: bool
    ) -> DirectorProjectRecord:
        async with self._database.write_session() as session:
            current = await _locked_project(session, project_id)
            _require_revision(current, expected_revision)
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    narration_enabled=int(enabled),
                    revision=expected_revision + 1,
                    role_revision=director_projects.c.role_revision + 1,
                    status="role_review",
                    updated_at_utc=_now(),
                )
            )
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.project_id == str(project_id))
                .where(director_utterances.c.kind == "narration")
                .values(speak_enabled=int(enabled), revision=director_utterances.c.revision + 1)
            )
            await _append_event(
                session,
                project_id,
                operation="narration_toggled",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"enabled": enabled},
            )
        return await self.get_project(project_id)

    async def patch_role(
        self,
        role_id: UUID,
        *,
        expected_revision: int,
        canonical_name: str | None = None,
        aliases: Sequence[str] | None = None,
    ) -> DirectorRoleRecord:
        values: dict[str, object] = {"revision": director_roles.c.revision + 1}
        if canonical_name is not None:
            if not canonical_name.strip():
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "role name must not be blank",
                    retryable=False,
                )
            values["canonical_name"] = canonical_name.strip()
        if aliases is not None:
            values["aliases_json"] = _json([item.strip() for item in aliases if item.strip()])
        async with self._database.write_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_roles).where(director_roles.c.role_id == str(role_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise KeyError(f"unknown director role: {role_id}")
            result = await session.execute(
                update(director_roles)
                .where(director_roles.c.role_id == str(role_id))
                .where(director_roles.c.revision == expected_revision)
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise _version_conflict()
            project_id = UUID(str(row["project_id"]))
            await _touch_project(session, project_id, role_change=True)
            await _append_event(
                session,
                project_id,
                operation="role_patched",
                object_id=role_id,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"fields": sorted(values)},
            )
        roles = await self.list_roles(project_id)
        return next(item for item in roles if item.role_id == role_id)

    async def bulk_assign_role(
        self,
        project_id: UUID,
        *,
        utterance_revisions: Mapping[UUID, int],
        role_id: UUID,
    ) -> list[DirectorUtteranceRecord]:
        if not utterance_revisions:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "select at least one utterance",
                retryable=False,
            )
        async with self._database.write_session() as session:
            role_exists = (
                await session.execute(
                    select(director_roles.c.role_id)
                    .where(director_roles.c.role_id == str(role_id))
                    .where(director_roles.c.project_id == str(project_id))
                )
            ).scalar_one_or_none()
            if role_exists is None:
                raise KeyError(f"unknown director role: {role_id}")
            for utterance_id, revision in utterance_revisions.items():
                result = await session.execute(
                    update(director_utterances)
                    .where(director_utterances.c.utterance_id == str(utterance_id))
                    .where(director_utterances.c.project_id == str(project_id))
                    .where(director_utterances.c.revision == revision)
                    .values(
                        role_id=str(role_id),
                        role_confirmed=1,
                        revision=director_utterances.c.revision + 1,
                    )
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    raise _version_conflict()
            await _touch_project(session, project_id, role_change=True)
            await _append_event(
                session,
                project_id,
                operation="utterances_reassigned",
                before_revision=0,
                after_revision=1,
                details={
                    "role_id": str(role_id),
                    "utterance_ids": [str(item) for item in utterance_revisions],
                },
            )
        return await self.list_utterances(project_id)

    async def merge_roles(
        self,
        project_id: UUID,
        *,
        source_role_ids: Sequence[UUID],
        target_role_id: UUID,
        expected_project_revision: int,
    ) -> list[DirectorRoleRecord]:
        sources = {str(item) for item in source_role_ids if item != target_role_id}
        if not sources:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "role merge requires a distinct source role",
                retryable=False,
            )
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_project_revision)
            rows = (
                (
                    await session.execute(
                        select(director_roles).where(director_roles.c.project_id == str(project_id))
                    )
                )
                .mappings()
                .all()
            )
            existing = {str(row["role_id"]) for row in rows}
            if str(target_role_id) not in existing or not sources <= existing:
                raise KeyError("unknown director role")
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.project_id == str(project_id))
                .where(director_utterances.c.role_id.in_(sources))
                .values(
                    role_id=str(target_role_id),
                    role_confirmed=1,
                    revision=director_utterances.c.revision + 1,
                )
            )
            await session.execute(
                delete(director_roles).where(director_roles.c.role_id.in_(sources))
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    revision=expected_project_revision + 1,
                    role_revision=director_projects.c.role_revision + 1,
                    status="role_review",
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="roles_merged",
                before_revision=expected_project_revision,
                after_revision=expected_project_revision + 1,
                details={
                    "target_role_id": str(target_role_id),
                    "source_role_ids": sorted(sources),
                },
            )
        return await self.list_roles(project_id)

    async def confirm_role_review(
        self, project_id: UUID, *, expected_revision: int
    ) -> DirectorProjectRecord:
        utterances = await self.list_utterances(project_id)
        roles = {item.role_id: item for item in await self.list_roles(project_id)}
        blockers = [
            item.utterance_id
            for item in utterances
            if item.speak_enabled
            and (
                not item.role_confirmed
                or item.role_id is None
                or item.role_id not in roles
                or roles[item.role_id].kind == "unknown"
            )
        ]
        if blockers:
            raise PipelineError(
                ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                "director",
                "all spoken utterances require a confirmed narrator or character",
                retryable=False,
                details={"utterance_ids": [str(item) for item in blockers]},
            )
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"role_review"},
            status="translating",
            event="role_review_confirmed",
        )
        return await self.get_project(project_id)

    async def publish_translation(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        items: Sequence[TranslationResultItem],
    ) -> DirectorProjectRecord:
        utterances = await self.list_utterances(project_id)
        spoken = {item.utterance_id: item for item in utterances if item.speak_enabled}
        results = {item.utterance_id: item for item in items}
        if set(results) != set(spoken):
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "director",
                "translation must contain every spoken utterance exactly once",
                retryable=False,
            )
        for utterance_id, item in results.items():
            if item.revision != spoken[utterance_id].revision:
                raise _version_conflict()
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project["status"]) != "translating":
                raise _state_conflict(str(project["status"]), "publish translation")
            for item in items:
                result = await session.execute(
                    update(director_utterances)
                    .where(director_utterances.c.utterance_id == str(item.utterance_id))
                    .where(director_utterances.c.revision == item.revision)
                    .values(
                        synthesis_text=item.synthesis_text,
                        ref_text_cn=item.ref_text_cn,
                        emotion_vector_json=_json(list(item.emotion_vector)),
                        speed_factor=item.speed_factor,
                        pause_after_ms=item.pause_after_ms,
                        revision=director_utterances.c.revision + 1,
                    )
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    raise _version_conflict()
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="translation_review",
                    revision=expected_revision + 1,
                    translation_revision=director_projects.c.translation_revision + 1,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="translation_published",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"utterances": len(items)},
            )
        return await self.get_project(project_id)

    async def confirm_translation(
        self, project_id: UUID, *, expected_revision: int
    ) -> DirectorProjectRecord:
        utterances = await self.list_utterances(project_id)
        invalid = [
            item.utterance_id
            for item in utterances
            if item.speak_enabled
            and (not item.synthesis_text or not item.ref_text_cn or item.emotion_vector is None)
        ]
        if invalid:
            raise PipelineError(
                ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                "director",
                "all spoken utterances require reviewed synthesis inputs",
                retryable=False,
                details={"utterance_ids": [str(item) for item in invalid]},
            )
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"translation_review"},
            status="voice_mapping",
            event="translation_confirmed",
        )
        return await self.get_project(project_id)

    async def bind_role_preset(
        self,
        role_id: UUID,
        *,
        expected_revision: int,
        preset_id: UUID,
    ) -> DirectorRoleRecord:
        async with self._database.write_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_roles).where(director_roles.c.role_id == str(role_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise KeyError(f"unknown director role: {role_id}")
            if int(row["revision"]) != expected_revision:
                raise _version_conflict()
            ready_preset = (
                await session.execute(
                    select(role_presets.c.preset_id)
                    .where(role_presets.c.preset_id == str(preset_id))
                    .where(role_presets.c.status == "ready")
                )
            ).scalar_one_or_none()
            if ready_preset is None:
                raise PipelineError(
                    ErrorCode.ROLE_PRESET_UNAVAILABLE,
                    "director",
                    "selected role preset is not ready",
                    retryable=False,
                )
            project_id = UUID(str(row["project_id"]))
            await session.execute(
                update(director_roles)
                .where(director_roles.c.role_id == str(role_id))
                .values(preset_id=str(preset_id), revision=director_roles.c.revision + 1)
            )
            missing = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(director_utterances.c.role_id)))
                        .select_from(
                            director_utterances.outerjoin(
                                director_roles,
                                director_utterances.c.role_id == director_roles.c.role_id,
                            )
                        )
                        .where(director_utterances.c.project_id == str(project_id))
                        .where(director_utterances.c.speak_enabled == 1)
                        .where(director_roles.c.preset_id.is_(None))
                    )
                ).scalar_one()
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="ready" if missing == 0 else "voice_mapping",
                    revision=director_projects.c.revision + 1,
                    mapping_revision=director_projects.c.mapping_revision + 1,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="role_preset_bound",
                object_id=role_id,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"preset_id": str(preset_id)},
            )
        roles = await self.list_roles(project_id)
        return next(item for item in roles if item.role_id == role_id)

    async def delete_project(self, project_id: UUID, *, expected_revision: int) -> None:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .where(director_projects.c.revision == expected_revision)
                .where(director_projects.c.deleted_at_utc.is_(None))
                .values(deleted_at_utc=_now(), revision=expected_revision + 1)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise _version_conflict()

    async def _update_project_state(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        allowed: set[str],
        status: str,
        event: str,
    ) -> None:
        async with self._database.write_session() as session:
            row = await _locked_project(session, project_id)
            _require_revision(row, expected_revision)
            if str(row["status"]) not in allowed:
                raise _state_conflict(str(row["status"]), event)
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(status=status, revision=expected_revision + 1, updated_at_utc=_now())
            )
            await _append_event(
                session,
                project_id,
                operation=event,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={},
            )


def validate_source_coverage(source: str, rows: Sequence[CreateDirectorUtterance]) -> None:
    cursor = 0
    for ordinal, row in enumerate(rows):
        if row.ordinal != ordinal or row.source_start != cursor:
            raise _coverage_error()
        if (
            row.source_end > len(source)
            or source[row.source_start : row.source_end] != row.source_text
        ):
            raise _coverage_error()
        cursor = row.source_end
    if cursor != len(source):
        raise _coverage_error()


async def _locked_project(session: Any, project_id: UUID) -> Mapping[str, Any]:
    row = (
        (
            await session.execute(
                select(director_projects).where(director_projects.c.project_id == str(project_id))
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise KeyError(f"unknown director project: {project_id}")
    return cast(Mapping[str, Any], row)


def _require_revision(row: Mapping[str, Any], expected: int) -> None:
    if int(row["revision"]) != expected:
        raise _version_conflict()


async def _touch_project(session: Any, project_id: UUID, *, role_change: bool) -> None:
    values: dict[str, Any] = {
        "revision": director_projects.c.revision + 1,
        "updated_at_utc": _now(),
    }
    if role_change:
        values.update(
            role_revision=director_projects.c.role_revision + 1,
            status="role_review",
        )
    else:
        values.update(
            translation_revision=director_projects.c.translation_revision + 1,
            status="translation_review",
        )
    await session.execute(
        update(director_projects)
        .where(director_projects.c.project_id == str(project_id))
        .values(**values)
    )


async def _append_event(
    session: Any,
    project_id: UUID,
    *,
    operation: str,
    before_revision: int,
    after_revision: int,
    details: dict[str, object],
    object_id: UUID | None = None,
) -> None:
    sequence = (
        int(
            (
                await session.execute(
                    select(func.coalesce(func.max(director_edit_events.c.sequence), 0)).where(
                        director_edit_events.c.project_id == str(project_id)
                    )
                )
            ).scalar_one()
        )
        + 1
    )
    await session.execute(
        insert(director_edit_events).values(
            event_id=str(uuid4()),
            project_id=str(project_id),
            sequence=sequence,
            operation=operation,
            object_id=str(object_id) if object_id else None,
            before_revision=before_revision,
            after_revision=after_revision,
            details_json=_json(details),
            created_at_utc=_now(),
        )
    )


def _project(row: dict[str, Any]) -> DirectorProjectRecord:
    def parsed(name: str) -> dict[str, Any] | None:
        value = row.get(name)
        return json.loads(str(value)) if value is not None else None

    return DirectorProjectRecord(
        project_id=UUID(str(row["project_id"])),
        title=str(row["title"]),
        source_text=str(row["source_text"]),
        source_text_sha256=str(row["source_text_sha256"]),
        source_language=str(row["source_language"]),  # type: ignore[arg-type]
        target_language=str(row["target_language"]),  # type: ignore[arg-type]
        narration_enabled=bool(row["narration_enabled"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        revision=int(row["revision"]),
        analysis_revision=int(row["analysis_revision"]),
        role_revision=int(row["role_revision"]),
        translation_revision=int(row["translation_revision"]),
        mapping_revision=int(row["mapping_revision"]),
        generation_revision=int(row["generation_revision"]),
        current_generation_id=(
            UUID(str(row["current_generation_id"])) if row.get("current_generation_id") else None
        ),
        final_relative_path=cast(str | None, row.get("final_relative_path")),
        timeline=parsed("timeline_json"),
        last_error=parsed("last_error_json"),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
        deleted_at_utc=(
            datetime.fromisoformat(str(row["deleted_at_utc"]))
            if row.get("deleted_at_utc")
            else None
        ),
    )


def _role(row: dict[str, Any]) -> DirectorRoleRecord:
    return DirectorRoleRecord(
        role_id=UUID(str(row["role_id"])),
        project_id=UUID(str(row["project_id"])),
        canonical_name=str(row["canonical_name"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        aliases=tuple(json.loads(str(row["aliases_json"]))),
        confidence=float(row["confidence"]),
        preset_id=UUID(str(row["preset_id"])) if row.get("preset_id") else None,
        revision=int(row["revision"]),
    )


def _utterance(row: dict[str, Any]) -> DirectorUtteranceRecord:
    vector = row.get("emotion_vector_json")
    return DirectorUtteranceRecord(
        utterance_id=UUID(str(row["utterance_id"])),
        project_id=UUID(str(row["project_id"])),
        ordinal=int(row["ordinal"]),
        source_start=int(row["source_start"]),
        source_end=int(row["source_end"]),
        source_text=str(row["source_text"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        speak_enabled=bool(row["speak_enabled"]),
        role_id=UUID(str(row["role_id"])) if row.get("role_id") else None,
        role_confidence=float(row["role_confidence"]),
        role_confirmed=bool(row["role_confirmed"]),
        synthesis_text=cast(str | None, row.get("synthesis_text")),
        ref_text_cn=cast(str | None, row.get("ref_text_cn")),
        emotion_vector=(cast(EmotionVector, tuple(json.loads(str(vector)))) if vector else None),
        speed_factor=float(row["speed_factor"]),
        pause_after_ms=int(row["pause_after_ms"]),
        seed=int(row["seed"]),
        revision=int(row["revision"]),
    )


def _coverage_error() -> PipelineError:
    return PipelineError(
        ErrorCode.DIRECTOR_SOURCE_COVERAGE_INVALID,
        "director",
        "analysis must cover the source exactly once in source order",
        retryable=False,
    )


def _version_conflict() -> PipelineError:
    return PipelineError(
        ErrorCode.VERSION_CONFLICT,
        "director",
        "director data changed; refresh before saving",
        retryable=False,
    )


def _state_conflict(status: str, operation: str) -> PipelineError:
    return PipelineError(
        ErrorCode.DIRECTOR_STATE_CONFLICT,
        "director",
        f"cannot {operation} while project is {status}",
        retryable=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stored_chunk_id(project_id: UUID, chunk_id: str) -> str:
    return hashlib.sha256(f"{project_id}:{chunk_id}".encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
