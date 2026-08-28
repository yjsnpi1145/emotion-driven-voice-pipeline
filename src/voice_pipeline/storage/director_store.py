from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateDirectorProjectRequest,
    CreateDirectorRole,
    CreateDirectorUtterance,
    DirectorGenerationItemRecord,
    DirectorGenerationRecord,
    DirectorPreprocessParagraphPage,
    DirectorPreprocessParagraphRecord,
    DirectorProjectRecord,
    DirectorRoleRecord,
    DirectorUtteranceRecord,
    PreprocessRewriteState,
)
from voice_pipeline.models.director_llm import (
    ChunkAnalysisResult,
    ScriptChunk,
    TranslationResultItem,
)
from voice_pipeline.models.schemas import EmotionVector
from voice_pipeline.modules.text.speakability import is_speakable_text
from voice_pipeline.modules.text.structural_cleaner import StructuralDocument
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import (
    director_analysis_chunks,
    director_edit_events,
    director_generation_items,
    director_generations,
    director_preprocess_paragraphs,
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
                    preprocessing_mode=request.preprocessing_mode,
                    performance_direction=request.performance_direction,
                    structural_text=None,
                    preprocessed_text=None,
                    status="draft",
                    revision=0,
                    preprocess_revision=0,
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

    async def update_performance_direction(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        performance_direction: str | None,
        reapply: bool,
    ) -> DirectorProjectRecord:
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            status = str(project["status"])
            editable_without_reapply = {"draft", "preprocess_review", "role_review"}
            if status not in editable_without_reapply and not reapply:
                raise _state_conflict(status, "edit performance direction")
            if status in {"preprocessing", "analyzing", "translating", "generating"}:
                raise _state_conflict(status, "edit performance direction")
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .where(director_projects.c.revision == expected_revision)
                .values(
                    performance_direction=performance_direction,
                    revision=director_projects.c.revision + 1,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="performance_direction_updated",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={
                    "reapply": reapply,
                    "has_direction": performance_direction is not None,
                },
            )
        return await self.get_project(project_id)

    async def health_counts(self) -> dict[str, int]:
        """Return path-free operational counts for the control-plane health payload."""
        async with self._database.read_session() as session:

            async def project_count(statuses: Sequence[str]) -> int:
                value = await session.scalar(
                    select(func.count())
                    .select_from(director_projects)
                    .where(director_projects.c.deleted_at_utc.is_(None))
                    .where(director_projects.c.status.in_(list(statuses)))
                )
                return int(value or 0)

            active_analysis = await project_count(("preprocessing", "analyzing", "translating"))
            active_generation = await project_count(("generating",))
            projects_needing_review = await project_count(
                (
                    "preprocess_review",
                    "role_review",
                    "translation_review",
                    "voice_mapping",
                    "generation_incomplete",
                )
            )
            unavailable = await session.scalar(
                select(func.count())
                .select_from(role_presets)
                .where(role_presets.c.status.not_in(["ready", "archived"]))
            )
        return {
            "active_analysis": active_analysis,
            "active_generation": active_generation,
            "projects_needing_review": projects_needing_review,
            "unavailable_role_presets": int(unavailable or 0),
        }

    async def begin_preprocessing(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
    ) -> DirectorProjectRecord:
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"draft", "preprocessing", "preprocess_review"},
            status="preprocessing",
            event="preprocessing_started",
        )
        return await self.get_project(project_id)

    async def stage_preprocess_document(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        document: StructuralDocument,
    ) -> None:
        now = _now()
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project["status"]) != "preprocessing":
                raise _state_conflict(str(project["status"]), "stage preprocessing")
            rewrite_mode = str(project["preprocessing_mode"]) == "rewrite"
            existing_rows = (
                (
                    await session.execute(
                        select(director_preprocess_paragraphs).where(
                            director_preprocess_paragraphs.c.project_id
                            == str(project_id)
                        )
                    )
                )
                .mappings()
                .all()
            )
            existing_by_id = {
                str(row["paragraph_id"]): row for row in existing_rows
            }
            incoming_ids = {
                paragraph.paragraph_id for paragraph in document.paragraphs
            }
            await session.execute(
                delete(director_preprocess_paragraphs).where(
                    director_preprocess_paragraphs.c.project_id == str(project_id),
                    director_preprocess_paragraphs.c.paragraph_id.not_in(incoming_ids),
                )
            )
            resumed = 0
            pending = 0
            for paragraph in document.paragraphs:
                source_sha = _sha256(paragraph.source_text)
                structural_sha = _sha256(paragraph.structural_text)
                desired_state: PreprocessRewriteState = (
                    "pending"
                    if rewrite_mode and any(unit.speakable for unit in paragraph.units)
                    else "local"
                )
                existing = existing_by_id.get(paragraph.paragraph_id)
                unchanged = bool(
                    existing is not None
                    and str(existing["source_sha256"]) == source_sha
                    and str(existing["structural_sha256"]) == structural_sha
                )
                preserved = False
                if unchanged and existing is not None:
                    preserved = str(existing["rewrite_state"]) in {
                        "succeeded",
                        "user_edited",
                        "local",
                    }
                common = {
                    "ordinal": paragraph.ordinal,
                    "source_start": paragraph.source_start,
                    "source_end": paragraph.source_end,
                    "source_text": paragraph.source_text,
                    "structural_text": paragraph.structural_text,
                    "source_sha256": source_sha,
                    "structural_sha256": structural_sha,
                    "updated_at_utc": now,
                }
                if existing is None:
                    await session.execute(
                        insert(director_preprocess_paragraphs).values(
                            paragraph_id=paragraph.paragraph_id,
                            project_id=str(project_id),
                            preprocessed_text=paragraph.structural_text,
                            rewrite_state=desired_state,
                            validation_json=None,
                            revision=0,
                            preprocessed_sha256=structural_sha,
                            created_at_utc=now,
                            **common,
                        )
                    )
                elif preserved:
                    resumed += 1
                    await session.execute(
                        update(director_preprocess_paragraphs)
                        .where(
                            director_preprocess_paragraphs.c.paragraph_id
                            == paragraph.paragraph_id
                        )
                        .values(**common)
                    )
                else:
                    new_revision = int(existing["revision"])
                    if str(existing["rewrite_state"]) != "pending" or not unchanged:
                        new_revision += 1
                    await session.execute(
                        update(director_preprocess_paragraphs)
                        .where(
                            director_preprocess_paragraphs.c.paragraph_id
                            == paragraph.paragraph_id
                        )
                        .values(
                            preprocessed_text=paragraph.structural_text,
                            rewrite_state=desired_state,
                            validation_json=None,
                            revision=new_revision,
                            preprocessed_sha256=structural_sha,
                            **common,
                        )
                    )
                if desired_state == "pending" and not preserved:
                    pending += 1
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    structural_text=document.structural_text,
                    preprocessed_text=None,
                    updated_at_utc=now,
                )
            )
            await _append_event(
                session,
                project_id,
                operation="preprocessing_staged",
                before_revision=expected_revision,
                after_revision=expected_revision,
                details={
                    "paragraphs": len(document.paragraphs),
                    "resumed": resumed,
                    "pending": pending,
                },
            )

    async def pending_preprocess_paragraphs(
        self,
        project_id: UUID,
    ) -> tuple[DirectorPreprocessParagraphRecord, ...]:
        await self.get_project(project_id)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_preprocess_paragraphs)
                        .where(
                            director_preprocess_paragraphs.c.project_id
                            == str(project_id),
                            director_preprocess_paragraphs.c.rewrite_state == "pending",
                        )
                        .order_by(director_preprocess_paragraphs.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_preprocess_paragraph(dict(row)) for row in rows)

    async def save_preprocess_result(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
        preprocessed_text: str,
        rewrite_state: Literal["succeeded", "fallback", "local"],
        validation: dict[str, object] | None = None,
    ) -> DirectorPreprocessParagraphRecord:
        if not preprocessed_text.strip():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "preprocessed paragraph must not be blank",
                retryable=False,
            )
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_project_revision)
            if str(project["status"]) != "preprocessing":
                raise _state_conflict(str(project["status"]), "save preprocessing result")
            paragraph = await _locked_preprocess_paragraph(session, paragraph_id)
            if str(paragraph["project_id"]) != str(project_id):
                raise KeyError(f"unknown preprocessing paragraph: {paragraph_id}")
            if int(paragraph["revision"]) != expected_revision:
                raise _version_conflict()
            await session.execute(
                update(director_preprocess_paragraphs)
                .where(director_preprocess_paragraphs.c.paragraph_id == paragraph_id)
                .values(
                    preprocessed_text=preprocessed_text,
                    rewrite_state=rewrite_state,
                    validation_json=_json(validation) if validation is not None else None,
                    revision=expected_revision + 1,
                    preprocessed_sha256=_sha256(preprocessed_text),
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation=(
                    "preprocessing_paragraph_fallback"
                    if rewrite_state == "fallback"
                    else "preprocessing_paragraph_saved"
                ),
                before_revision=expected_project_revision,
                after_revision=expected_project_revision,
                details={
                    "paragraph_id": paragraph_id,
                    "paragraph_revision": expected_revision + 1,
                    "rewrite_state": rewrite_state,
                },
            )
        return await self.get_preprocess_paragraph(project_id, paragraph_id)

    async def complete_preprocessing(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
    ) -> DirectorProjectRecord:
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project["status"]) != "preprocessing":
                raise _state_conflict(str(project["status"]), "complete preprocessing")
            rows = (
                (
                    await session.execute(
                        select(
                            director_preprocess_paragraphs.c.preprocessed_text,
                            director_preprocess_paragraphs.c.rewrite_state,
                        )
                        .where(
                            director_preprocess_paragraphs.c.project_id == str(project_id)
                        )
                        .order_by(director_preprocess_paragraphs.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            if not rows or any(str(row["rewrite_state"]) == "pending" for row in rows):
                raise _state_conflict("preprocessing", "complete unresolved preprocessing")
            preprocessed_text = "\n\n".join(str(row["preprocessed_text"]) for row in rows)
            if not preprocessed_text.strip():
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "preprocessed document must not be blank",
                    retryable=False,
                )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    preprocessed_text=preprocessed_text,
                    status="preprocess_review",
                    revision=expected_revision + 1,
                    preprocess_revision=director_projects.c.preprocess_revision + 1,
                    last_error_json=None,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="preprocessing_completed",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={
                    "paragraphs": len(rows),
                    "fallbacks": sum(
                        str(row["rewrite_state"]) == "fallback" for row in rows
                    ),
                },
            )
        return await self.get_project(project_id)

    async def list_preprocess_paragraphs(
        self,
        project_id: UUID,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> DirectorPreprocessParagraphPage:
        await self.get_project(project_id)
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid preprocessing pagination")
        async with self._database.read_session() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(director_preprocess_paragraphs)
                        .where(
                            director_preprocess_paragraphs.c.project_id == str(project_id)
                        )
                    )
                ).scalar_one()
            )
            rows = (
                (
                    await session.execute(
                        select(director_preprocess_paragraphs)
                        .where(
                            director_preprocess_paragraphs.c.project_id == str(project_id)
                        )
                        .order_by(director_preprocess_paragraphs.c.ordinal)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        items = tuple(_preprocess_paragraph(dict(row)) for row in rows)
        consumed = offset + len(items)
        return DirectorPreprocessParagraphPage(
            items=items,
            total_count=total,
            next_offset=consumed if consumed < total else None,
        )

    async def get_preprocess_paragraph(
        self,
        project_id: UUID,
        paragraph_id: str,
    ) -> DirectorPreprocessParagraphRecord:
        await self.get_project(project_id)
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_preprocess_paragraphs).where(
                            director_preprocess_paragraphs.c.paragraph_id == paragraph_id,
                            director_preprocess_paragraphs.c.project_id == str(project_id),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown preprocessing paragraph: {paragraph_id}")
        return _preprocess_paragraph(dict(row))

    async def patch_preprocess_paragraph(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
        preprocessed_text: str,
    ) -> DirectorPreprocessParagraphRecord:
        return await self._edit_preprocess_paragraph(
            project_id,
            paragraph_id,
            expected_project_revision=expected_project_revision,
            expected_revision=expected_revision,
            preprocessed_text=preprocessed_text,
            rewrite_state="user_edited",
            operation="preprocessing_paragraph_edited",
        )

    async def restore_preprocess_paragraph(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
        target: Literal["source", "structural"],
    ) -> DirectorPreprocessParagraphRecord:
        paragraph = await self.get_preprocess_paragraph(project_id, paragraph_id)
        value = (
            paragraph.source_text if target == "source" else paragraph.structural_text
        )
        return await self._edit_preprocess_paragraph(
            project_id,
            paragraph_id,
            expected_project_revision=expected_project_revision,
            expected_revision=expected_revision,
            preprocessed_text=value,
            rewrite_state="user_edited",
            operation="preprocessing_paragraph_restored",
            details={"target": target},
        )

    async def apply_review_preprocess_result(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
        preprocessed_text: str,
        rewrite_state: Literal["succeeded", "fallback"],
        validation: dict[str, object] | None = None,
    ) -> DirectorPreprocessParagraphRecord:
        return await self._edit_preprocess_paragraph(
            project_id,
            paragraph_id,
            expected_project_revision=expected_project_revision,
            expected_revision=expected_revision,
            preprocessed_text=preprocessed_text,
            rewrite_state=rewrite_state,
            operation=(
                "preprocessing_paragraph_fallback"
                if rewrite_state == "fallback"
                else "preprocessing_paragraph_rewritten"
            ),
            validation=validation,
        )

    async def _edit_preprocess_paragraph(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
        preprocessed_text: str,
        rewrite_state: PreprocessRewriteState,
        operation: str,
        details: dict[str, object] | None = None,
        validation: dict[str, object] | None = None,
    ) -> DirectorPreprocessParagraphRecord:
        if not preprocessed_text.strip():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "preprocessed paragraph must not be blank",
                retryable=False,
            )
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_project_revision)
            if str(project["status"]) != "preprocess_review":
                raise _state_conflict(str(project["status"]), operation)
            paragraph = await _locked_preprocess_paragraph(session, paragraph_id)
            if str(paragraph["project_id"]) != str(project_id):
                raise KeyError(f"unknown preprocessing paragraph: {paragraph_id}")
            if int(paragraph["revision"]) != expected_revision:
                raise _version_conflict()
            await session.execute(
                update(director_preprocess_paragraphs)
                .where(director_preprocess_paragraphs.c.paragraph_id == paragraph_id)
                .values(
                    preprocessed_text=preprocessed_text,
                    preprocessed_sha256=_sha256(preprocessed_text),
                    rewrite_state=rewrite_state,
                    validation_json=(
                        _json(validation) if validation is not None else None
                    ),
                    revision=expected_revision + 1,
                    updated_at_utc=_now(),
                )
            )
            rows = (
                (
                    await session.execute(
                        select(director_preprocess_paragraphs.c.preprocessed_text)
                        .where(
                            director_preprocess_paragraphs.c.project_id == str(project_id)
                        )
                        .order_by(director_preprocess_paragraphs.c.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            recomposed = "\n\n".join(str(value) for value in rows)
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    preprocessed_text=recomposed,
                    revision=expected_project_revision + 1,
                    preprocess_revision=director_projects.c.preprocess_revision + 1,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation=operation,
                before_revision=expected_project_revision,
                after_revision=expected_project_revision + 1,
                details={
                    "paragraph_id": paragraph_id,
                    **(details or {}),
                },
            )
        return await self.get_preprocess_paragraph(project_id, paragraph_id)

    async def confirm_preprocessing(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
    ) -> DirectorProjectRecord:
        project = await self.get_project(project_id)
        if project.preprocessed_text is None or not is_speakable_text(
            project.preprocessed_text
        ):
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "preprocessed document contains no speakable text",
                retryable=False,
            )
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"preprocess_review"},
            status="analyzing",
            event="preprocessing_confirmed",
        )
        return await self.get_project(project_id)

    async def analysis_text(self, project_id: UUID) -> str:
        project = await self.get_project(project_id)
        if project.preprocessed_text is None:
            raise PipelineError(
                ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                "director",
                "confirm the preprocessing draft before analysis",
                retryable=False,
            )
        return project.preprocessed_text

    async def begin_analysis(
        self, project_id: UUID, *, expected_revision: int
    ) -> DirectorProjectRecord:
        await self._update_project_state(
            project_id,
            expected_revision=expected_revision,
            allowed={"analyzing", "role_review"},
            status="analyzing",
            event="analysis_started",
        )
        return await self.get_project(project_id)

    async def record_command_failure(
        self,
        project_id: UUID,
        *,
        expected_status: str,
        operation: str,
        error: dict[str, object],
    ) -> bool:
        """Persist a background-command failure without overwriting later edits."""
        async with self._database.write_session() as session:
            row = await _locked_project(session, project_id)
            if str(row["status"]) != expected_status:
                return False
            revision = int(row["revision"])
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .where(director_projects.c.status == expected_status)
                .values(last_error_json=_json(error), updated_at_utc=_now())
            )
            await _append_event(
                session,
                project_id,
                operation=f"{operation}_failed",
                before_revision=revision,
                after_revision=revision,
                details={"error": error},
            )
        return True

    async def recover_interrupted_commands(self) -> tuple[UUID, ...]:
        """Make LLM stages left active by a previous process visibly retryable."""
        error = {
            "code": "DIRECTOR_COMMAND_INTERRUPTED",
            "stage": "director",
            "message": "director command was interrupted by a service restart",
            "retryable": True,
            "details": {},
        }
        recovered: list[UUID] = []
        async with self._database.write_session() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            director_projects.c.project_id,
                            director_projects.c.revision,
                            director_projects.c.status,
                        ).where(
                            director_projects.c.deleted_at_utc.is_(None),
                            director_projects.c.status.in_(
                                ["preprocessing", "analyzing", "translating"]
                            ),
                            director_projects.c.last_error_json.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                project_id = UUID(str(row["project_id"]))
                recovered.append(project_id)
                await session.execute(
                    update(director_projects)
                    .where(director_projects.c.project_id == str(project_id))
                    .values(last_error_json=_json(error), updated_at_utc=_now())
                )
                await _append_event(
                    session,
                    project_id,
                    operation=f"{row['status']}_interrupted",
                    before_revision=int(row["revision"]),
                    after_revision=int(row["revision"]),
                    details={"error": error},
                )
        return tuple(recovered)

    async def load_analysis_chunk(
        self,
        project_id: UUID,
        chunk: ScriptChunk,
        *,
        llm_fingerprint: str,
        prompt_version: str,
        schema_version: int,
    ) -> ChunkAnalysisResult | None:
        chunk_id = _stored_chunk_id(project_id, chunk.chunk_id)
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(
                            director_analysis_chunks.c.source_sha256,
                            director_analysis_chunks.c.llm_fingerprint,
                            director_analysis_chunks.c.prompt_version,
                            director_analysis_chunks.c.schema_version,
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
            or str(row["llm_fingerprint"]) != llm_fingerprint
            or str(row["prompt_version"]) != prompt_version
            or int(row["schema_version"]) != schema_version
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
        prompt_version: str,
        schema_version: int,
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
                "prompt_version": prompt_version,
                "schema_version": schema_version,
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
        if project.preprocessed_text is None:
            raise PipelineError(
                ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                "director",
                "confirm the preprocessing draft before publishing analysis",
                retryable=False,
            )
        validate_source_coverage(project.preprocessed_text, utterances)
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
            if str(current["status"]) not in {"analyzing", "role_review"}:
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
            preprocess_rows = (
                (
                    await session.execute(
                        select(
                            director_preprocess_paragraphs.c.paragraph_id,
                            director_preprocess_paragraphs.c.preprocessed_text,
                        )
                        .where(
                            director_preprocess_paragraphs.c.project_id
                            == str(project_id)
                        )
                        .order_by(director_preprocess_paragraphs.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            preprocess_spans = _preprocess_paragraph_spans(
                dict(row) for row in preprocess_rows
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
                        "preprocess_paragraph_id": (
                            item.preprocess_paragraph_id
                            or _paragraph_id_for_range(
                                preprocess_spans,
                                item.source_start,
                                item.source_end,
                            )
                        ),
                        "working_text": item.source_text,
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
            "working_text",
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
        if "working_text" in values:
            if not str(values["working_text"]).strip():
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "working text must not be blank",
                    retryable=False,
                )
            values.update(
                synthesis_text=None,
                ref_text_cn=None,
                emotion_vector_json=None,
                task_id=None,
                segment_id=None,
                reference_version_id=None,
                gsv_version_id=None,
            )
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
            project_id = UUID(str(existing["project_id"]))
            project = await _locked_project(session, project_id)
            if "working_text" in values and str(project["status"]) != "role_review":
                raise _state_conflict(str(project["status"]), "edit working text")
            result = await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .where(director_utterances.c.revision == expected_revision)
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise _version_conflict()
            role_change = bool(
                {"role_id", "speak_enabled", "role_confirmed", "working_text"} & values.keys()
            )
            await _touch_project(session, project_id, role_change=role_change)
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
            working = str(row["working_text"])
            if working != source:
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "restore the working text before splitting the original source slice",
                    retryable=False,
                )
            cut = split_at - start
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .values(
                    source_end=split_at,
                    source_text=source[:cut],
                    working_text=working[:cut],
                    synthesis_text=None,
                    ref_text_cn=None,
                    emotion_vector_json=None,
                    task_id=None,
                    segment_id=None,
                    reference_version_id=None,
                    gsv_version_id=None,
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
                working_text=working[cut:],
                synthesis_text=None,
                ref_text_cn=None,
                emotion_vector_json=None,
                task_id=None,
                segment_id=None,
                reference_version_id=None,
                gsv_version_id=None,
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
                    working_text=str(left["working_text"]) + str(right["working_text"]),
                    preprocess_paragraph_id=(
                        left["preprocess_paragraph_id"]
                        if left["preprocess_paragraph_id"]
                        == right["preprocess_paragraph_id"]
                        else None
                    ),
                    synthesis_text=None,
                    ref_text_cn=None,
                    emotion_vector_json=None,
                    task_id=None,
                    segment_id=None,
                    reference_version_id=None,
                    gsv_version_id=None,
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

    async def split_role(
        self,
        project_id: UUID,
        *,
        source_role_id: UUID,
        utterance_ids: Sequence[UUID],
        canonical_name: str,
        expected_project_revision: int,
    ) -> list[DirectorRoleRecord]:
        selected = {str(item) for item in utterance_ids}
        if not selected or not canonical_name.strip():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "role split requires selected utterances and a new name",
                retryable=False,
            )
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_project_revision)
            if str(project["status"]) != "role_review":
                raise _state_conflict(str(project["status"]), "split role")
            source = (
                (
                    await session.execute(
                        select(director_roles).where(
                            director_roles.c.role_id == str(source_role_id),
                            director_roles.c.project_id == str(project_id),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise KeyError("unknown director role")
            duplicate = await session.scalar(
                select(director_roles.c.role_id).where(
                    director_roles.c.project_id == str(project_id),
                    director_roles.c.canonical_name == canonical_name.strip(),
                )
            )
            if duplicate is not None:
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "director role name already exists",
                    retryable=False,
                )
            rows = (
                (
                    await session.execute(
                        select(
                            director_utterances.c.utterance_id,
                            director_utterances.c.role_id,
                        ).where(
                            director_utterances.c.project_id == str(project_id),
                            director_utterances.c.utterance_id.in_(selected),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) != len(selected) or any(
                str(row["role_id"]) != str(source_role_id) for row in rows
            ):
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "selected utterances do not all belong to the source role",
                    retryable=False,
                )
            new_role_id = uuid4()
            await session.execute(
                insert(director_roles).values(
                    role_id=str(new_role_id),
                    project_id=str(project_id),
                    canonical_name=canonical_name.strip(),
                    kind="character",
                    aliases_json="[]",
                    confidence=1.0,
                    preset_id=None,
                    revision=0,
                )
            )
            await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id.in_(selected))
                .values(
                    role_id=str(new_role_id),
                    role_confirmed=1,
                    revision=director_utterances.c.revision + 1,
                )
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    revision=expected_project_revision + 1,
                    role_revision=director_projects.c.role_revision + 1,
                    updated_at_utc=_now(),
                )
            )
            await _append_event(
                session,
                project_id,
                operation="role_split",
                before_revision=expected_project_revision,
                after_revision=expected_project_revision + 1,
                details={
                    "source_role_id": str(source_role_id),
                    "new_role_id": str(new_role_id),
                    "utterance_ids": sorted(selected),
                },
            )
        return await self.list_roles(project_id)

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
        preset_id: UUID | None,
        dubbing_enabled: bool = True,
    ) -> DirectorRoleRecord:
        if dubbing_enabled != (preset_id is not None):
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "enabled role mapping requires a preset; skipped mapping forbids one",
                retryable=False,
            )
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
            if dubbing_enabled:
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
                .values(
                    preset_id=str(preset_id) if preset_id is not None else None,
                    dubbing_enabled=int(dubbing_enabled),
                    revision=director_roles.c.revision + 1,
                )
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
                        .where(
                            or_(
                                director_roles.c.role_id.is_(None),
                                and_(
                                    director_roles.c.dubbing_enabled == 1,
                                    director_roles.c.preset_id.is_(None),
                                ),
                            )
                        )
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
                details={
                    "preset_id": str(preset_id) if preset_id is not None else None,
                    "dubbing_enabled": dubbing_enabled,
                },
            )
        roles = await self.list_roles(project_id)
        return next(item for item in roles if item.role_id == role_id)

    async def prepare_generation(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        snapshot: dict[str, object],
        items: Sequence[tuple[UUID, int, UUID]],
    ) -> DirectorGenerationRecord:
        async with self._database.write_session() as session:
            existing = (
                (
                    await session.execute(
                        select(director_generations).where(
                            director_generations.c.project_id == str(project_id),
                            director_generations.c.project_revision == expected_revision,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return _generation(dict(existing))
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project["status"]) != "ready":
                raise _state_conflict(str(project["status"]), "start generation")
            generation_id = uuid4()
            now = _now()
            await session.execute(
                insert(director_generations).values(
                    generation_id=str(generation_id),
                    project_id=str(project_id),
                    project_revision=expected_revision,
                    status="queued",
                    snapshot_json=_json(snapshot),
                    final_relative_path=None,
                    timeline_json=None,
                    error_json=None,
                    created_at_utc=now,
                    started_at_utc=None,
                    finished_at_utc=None,
                )
            )
            if items:
                await session.execute(
                    insert(director_generation_items),
                    [
                        {
                            "generation_id": str(generation_id),
                            "utterance_id": str(utterance_id),
                            "ordinal": ordinal,
                            "model_profile_id": str(profile_id),
                            "status": "queued",
                            "reference_job_id": None,
                            "gsv_job_id": None,
                            "error_json": None,
                        }
                        for utterance_id, ordinal, profile_id in items
                    ],
                )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="generating",
                    revision=expected_revision + 1,
                    generation_revision=director_projects.c.generation_revision + 1,
                    current_generation_id=str(generation_id),
                    updated_at_utc=now,
                )
            )
            await _append_event(
                session,
                project_id,
                operation="generation_prepared",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"generation_id": str(generation_id), "items": len(items)},
            )
        return await self.get_generation(generation_id)

    async def get_generation(self, generation_id: UUID) -> DirectorGenerationRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_generations).where(
                            director_generations.c.generation_id == str(generation_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown director generation: {generation_id}")
        return _generation(dict(row))

    async def current_generation(self, project_id: UUID) -> DirectorGenerationRecord | None:
        project = await self.get_project(project_id)
        if project.current_generation_id is None:
            return None
        return await self.get_generation(project.current_generation_id)

    async def list_generation_items(
        self, generation_id: UUID
    ) -> list[DirectorGenerationItemRecord]:
        await self.get_generation(generation_id)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(director_generation_items)
                        .where(director_generation_items.c.generation_id == str(generation_id))
                        .order_by(director_generation_items.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
        return [_generation_item(dict(row)) for row in rows]

    async def mark_generation_running(self, generation_id: UUID) -> None:
        async with self._database.write_session() as session:
            await session.execute(
                update(director_generations)
                .where(director_generations.c.generation_id == str(generation_id))
                .where(director_generations.c.status.in_(["queued", "interrupted"]))
                .values(status="running", started_at_utc=_now(), error_json=None)
            )

    async def reopen_generation(
        self,
        project_id: UUID,
        generation_id: UUID,
        *,
        expected_revision: int,
    ) -> DirectorGenerationRecord:
        """Atomically make an incomplete/interrupted generation runnable again."""
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project.get("current_generation_id")) != str(generation_id):
                raise _state_conflict(str(project["status"]), "resume an older generation")
            generation = (
                (
                    await session.execute(
                        select(director_generations).where(
                            director_generations.c.generation_id == str(generation_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if generation is None:
                raise KeyError(f"unknown director generation: {generation_id}")
            if str(generation["status"]) not in {"generation_incomplete", "interrupted"}:
                raise _state_conflict(str(generation["status"]), "resume generation")
            now = _now()
            await session.execute(
                update(director_generations)
                .where(director_generations.c.generation_id == str(generation_id))
                .values(status="queued", error_json=None, finished_at_utc=None)
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="generating",
                    revision=expected_revision + 1,
                    last_error_json=None,
                    updated_at_utc=now,
                )
            )
            await _append_event(
                session,
                project_id,
                operation="generation_resumed",
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"generation_id": str(generation_id)},
            )
        return await self.get_generation(generation_id)

    async def begin_generation_adjustment(
        self,
        project_id: UUID,
        generation_id: UUID,
        *,
        expected_revision: int,
        utterance_id: UUID,
        action: str,
    ) -> DirectorGenerationRecord:
        async with self._database.write_session() as session:
            project = await _locked_project(session, project_id)
            _require_revision(project, expected_revision)
            if str(project.get("current_generation_id")) != str(generation_id):
                raise _state_conflict(str(project["status"]), "adjust an older generation")
            generation = (
                (
                    await session.execute(
                        select(director_generations).where(
                            director_generations.c.generation_id == str(generation_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if generation is None:
                raise KeyError(f"unknown director generation: {generation_id}")
            if str(generation["status"]) not in {"succeeded", "generation_incomplete"}:
                raise _state_conflict(str(generation["status"]), "adjust generation")
            now = _now()
            await session.execute(
                update(director_generations)
                .where(director_generations.c.generation_id == str(generation_id))
                .values(status="running", error_json=None, finished_at_utc=None)
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(project_id))
                .values(
                    status="generating",
                    revision=expected_revision + 1,
                    last_error_json=None,
                    updated_at_utc=now,
                )
            )
            await _append_event(
                session,
                project_id,
                operation="utterance_adjustment_started",
                object_id=utterance_id,
                before_revision=expected_revision,
                after_revision=expected_revision + 1,
                details={"generation_id": str(generation_id), "action": action},
            )
        return await self.get_generation(generation_id)

    async def mark_generation_interrupted(
        self, generation_id: UUID, *, error: dict[str, object]
    ) -> None:
        generation = await self.get_generation(generation_id)
        now = _now()
        async with self._database.write_session() as session:
            await session.execute(
                update(director_generations)
                .where(director_generations.c.generation_id == str(generation_id))
                .where(director_generations.c.status.in_(["queued", "running"]))
                .values(
                    status="interrupted",
                    error_json=_json(error),
                    finished_at_utc=now,
                )
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(generation.project_id))
                .where(director_projects.c.current_generation_id == str(generation_id))
                .values(
                    status="generation_incomplete",
                    last_error_json=_json(error),
                    updated_at_utc=now,
                )
            )

    async def set_generation_item(
        self,
        generation_id: UUID,
        utterance_id: UUID,
        *,
        status: str,
        reference_job_id: UUID | None = None,
        gsv_job_id: UUID | None = None,
        error: dict[str, object] | None = None,
        reference_mode: str | None = None,
        reference_pool_entry_id: UUID | None = None,
        reference_emotion_bucket: str | None = None,
        reference_degraded_from: str | None = None,
    ) -> None:
        values: dict[str, object | None] = {
            "status": status,
            "error_json": _json(error) if error is not None else None,
        }
        if reference_job_id is not None:
            values["reference_job_id"] = str(reference_job_id)
        if gsv_job_id is not None:
            values["gsv_job_id"] = str(gsv_job_id)
        if reference_mode is not None:
            values["reference_mode"] = reference_mode
            values["reference_pool_entry_id"] = (
                str(reference_pool_entry_id) if reference_pool_entry_id else None
            )
            values["reference_emotion_bucket"] = reference_emotion_bucket
            values["reference_degraded_from"] = reference_degraded_from
        async with self._database.write_session() as session:
            result = await session.execute(
                update(director_generation_items)
                .where(director_generation_items.c.generation_id == str(generation_id))
                .where(director_generation_items.c.utterance_id == str(utterance_id))
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError("unknown director generation item")

    async def attach_materialized_segment(
        self, utterance_id: UUID, *, task_id: UUID, segment_id: UUID
    ) -> None:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .values(task_id=str(task_id), segment_id=str(segment_id))
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"unknown director utterance: {utterance_id}")

    async def attach_utterance_versions(
        self,
        utterance_id: UUID,
        *,
        reference_version_id: UUID | None = None,
        gsv_version_id: UUID | None = None,
    ) -> None:
        values: dict[str, object] = {}
        if reference_version_id is not None:
            values["reference_version_id"] = str(reference_version_id)
        if gsv_version_id is not None:
            values["gsv_version_id"] = str(gsv_version_id)
        if not values:
            return
        async with self._database.write_session() as session:
            result = await session.execute(
                update(director_utterances)
                .where(director_utterances.c.utterance_id == str(utterance_id))
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"unknown director utterance: {utterance_id}")

    async def finish_generation(
        self,
        generation_id: UUID,
        *,
        succeeded: bool,
        final_relative_path: str | None = None,
        timeline: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> DirectorGenerationRecord:
        generation = await self.get_generation(generation_id)
        status = "succeeded" if succeeded else "generation_incomplete"
        now = _now()
        async with self._database.write_session() as session:
            await session.execute(
                update(director_generations)
                .where(director_generations.c.generation_id == str(generation_id))
                .values(
                    status=status,
                    final_relative_path=final_relative_path,
                    timeline_json=_json(timeline) if timeline is not None else None,
                    error_json=_json(error) if error is not None else None,
                    finished_at_utc=now,
                )
            )
            await session.execute(
                update(director_projects)
                .where(director_projects.c.project_id == str(generation.project_id))
                .values(
                    status=status,
                    final_relative_path=final_relative_path,
                    timeline_json=_json(timeline) if timeline is not None else None,
                    last_error_json=_json(error) if error is not None else None,
                    updated_at_utc=now,
                )
            )
        return await self.get_generation(generation_id)

    async def mark_running_generations_interrupted(self) -> tuple[UUID, ...]:
        async with self._database.write_session() as session:
            ids = [
                UUID(str(value))
                for value in (
                    await session.execute(
                        select(director_generations.c.generation_id).where(
                            director_generations.c.status.in_(["queued", "running"])
                        )
                    )
                ).scalars()
            ]
            if ids:
                await session.execute(
                    update(director_generations)
                    .where(director_generations.c.generation_id.in_([str(item) for item in ids]))
                    .values(status="interrupted", finished_at_utc=_now())
                )
                await session.execute(
                    update(director_projects)
                    .where(
                        director_projects.c.current_generation_id.in_([str(item) for item in ids])
                    )
                    .values(status="generation_incomplete", updated_at_utc=_now())
                )
        return tuple(ids)

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
                .values(
                    status=status,
                    revision=expected_revision + 1,
                    last_error_json=None,
                    updated_at_utc=_now(),
                )
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


async def _locked_preprocess_paragraph(
    session: Any,
    paragraph_id: str,
) -> Mapping[str, Any]:
    row = (
        (
            await session.execute(
                select(director_preprocess_paragraphs).where(
                    director_preprocess_paragraphs.c.paragraph_id == paragraph_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise KeyError(f"unknown preprocessing paragraph: {paragraph_id}")
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


def _preprocess_paragraph_spans(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[tuple[int, int, str], ...]:
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for row in rows:
        text = str(row["preprocessed_text"])
        end = cursor + len(text)
        spans.append((cursor, end, str(row["paragraph_id"])))
        cursor = end + 2
    return tuple(spans)


def _paragraph_id_for_range(
    spans: Sequence[tuple[int, int, str]],
    source_start: int,
    source_end: int,
) -> str | None:
    if not spans:
        return None
    best = max(
        spans,
        key=lambda span: max(
            0,
            min(source_end, span[1]) - max(source_start, span[0]),
        ),
    )
    overlap = max(0, min(source_end, best[1]) - max(source_start, best[0]))
    if overlap:
        return best[2]
    for start, _end, paragraph_id in spans:
        if source_start <= start:
            return paragraph_id
    return spans[-1][2]


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
        preprocessing_mode=str(row["preprocessing_mode"]),  # type: ignore[arg-type]
        performance_direction=cast(str | None, row.get("performance_direction")),
        structural_text=cast(str | None, row.get("structural_text")),
        preprocessed_text=cast(str | None, row.get("preprocessed_text")),
        status=str(row["status"]),  # type: ignore[arg-type]
        revision=int(row["revision"]),
        preprocess_revision=int(row["preprocess_revision"]),
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


def _preprocess_paragraph(row: dict[str, Any]) -> DirectorPreprocessParagraphRecord:
    validation = row.get("validation_json")
    return DirectorPreprocessParagraphRecord(
        paragraph_id=str(row["paragraph_id"]),
        project_id=UUID(str(row["project_id"])),
        ordinal=int(row["ordinal"]),
        source_start=int(row["source_start"]),
        source_end=int(row["source_end"]),
        source_text=str(row["source_text"]),
        structural_text=str(row["structural_text"]),
        preprocessed_text=str(row["preprocessed_text"]),
        rewrite_state=str(row["rewrite_state"]),  # type: ignore[arg-type]
        validation=(
            cast(dict[str, Any], json.loads(str(validation)))
            if validation is not None
            else None
        ),
        revision=int(row["revision"]),
        source_sha256=str(row["source_sha256"]),
        structural_sha256=str(row["structural_sha256"]),
        preprocessed_sha256=str(row["preprocessed_sha256"]),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
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
        dubbing_enabled=bool(row.get("dubbing_enabled", True)),
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
        preprocess_paragraph_id=cast(
            str | None, row.get("preprocess_paragraph_id")
        ),
        working_text=str(row["working_text"]),
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
        task_id=UUID(str(row["task_id"])) if row.get("task_id") else None,
        segment_id=UUID(str(row["segment_id"])) if row.get("segment_id") else None,
        reference_version_id=(
            UUID(str(row["reference_version_id"])) if row.get("reference_version_id") else None
        ),
        gsv_version_id=(UUID(str(row["gsv_version_id"])) if row.get("gsv_version_id") else None),
    )


def _generation(row: dict[str, Any]) -> DirectorGenerationRecord:
    def payload(name: str) -> dict[str, Any] | None:
        raw = row.get(name)
        return json.loads(str(raw)) if raw is not None else None

    return DirectorGenerationRecord(
        generation_id=UUID(str(row["generation_id"])),
        project_id=UUID(str(row["project_id"])),
        project_revision=int(row["project_revision"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        snapshot=json.loads(str(row["snapshot_json"])),
        final_relative_path=cast(str | None, row.get("final_relative_path")),
        timeline=payload("timeline_json"),
        error=payload("error_json"),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        started_at_utc=(
            datetime.fromisoformat(str(row["started_at_utc"]))
            if row.get("started_at_utc")
            else None
        ),
        finished_at_utc=(
            datetime.fromisoformat(str(row["finished_at_utc"]))
            if row.get("finished_at_utc")
            else None
        ),
    )


def _generation_item(row: dict[str, Any]) -> DirectorGenerationItemRecord:
    return DirectorGenerationItemRecord(
        generation_id=UUID(str(row["generation_id"])),
        utterance_id=UUID(str(row["utterance_id"])),
        ordinal=int(row["ordinal"]),
        model_profile_id=UUID(str(row["model_profile_id"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        reference_job_id=(
            UUID(str(row["reference_job_id"])) if row.get("reference_job_id") else None
        ),
        gsv_job_id=UUID(str(row["gsv_job_id"])) if row.get("gsv_job_id") else None,
        error=(json.loads(str(row["error_json"])) if row.get("error_json") else None),
        reference_mode=str(row.get("reference_mode") or "independent"),  # type: ignore[arg-type]
        reference_pool_entry_id=(
            UUID(str(row["reference_pool_entry_id"]))
            if row.get("reference_pool_entry_id")
            else None
        ),
        reference_emotion_bucket=(
            str(row["reference_emotion_bucket"])
            if row.get("reference_emotion_bucket")
            else None
        ),  # type: ignore[arg-type]
        reference_degraded_from=(
            str(row["reference_degraded_from"])
            if row.get("reference_degraded_from")
            else None
        ),  # type: ignore[arg-type]
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
