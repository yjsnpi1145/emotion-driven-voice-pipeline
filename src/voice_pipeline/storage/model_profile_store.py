from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.model_profiles import (
    ModelProfileRecord,
    ModelProfileSnapshot,
    ModelProfileStatus,
    ModelProfileView,
)
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import model_profiles, project_settings

_ACTIVE_PROFILE_SETTING = "active_gsv_model_profile_id"


class SqliteModelProfileStore:
    def __init__(self, database: Database, *, models_root: Path) -> None:
        self._database = database
        self._models_root = models_root.resolve()

    async def insert_published(self, record: ModelProfileRecord) -> ModelProfileSnapshot:
        async with self._database.write_session() as session:
            await session.execute(
                insert(model_profiles).values(
                    profile_id=str(record.profile_id),
                    display_name=record.display_name,
                    source_kind=record.source_kind,
                    declared_family=record.declared_family,
                    relative_directory=record.relative_directory.as_posix(),
                    gpt_relative_path=record.gpt_relative_path.as_posix(),
                    sovits_relative_path=record.sovits_relative_path.as_posix(),
                    gpt_sha256=record.gpt_sha256,
                    sovits_sha256=record.sovits_sha256,
                    gpt_size_bytes=record.gpt_size_bytes,
                    sovits_size_bytes=record.sovits_size_bytes,
                    status=record.status,
                    created_at_utc=record.created_at_utc.isoformat(),
                    archived_at_utc=(
                        record.archived_at_utc.isoformat()
                        if record.archived_at_utc is not None
                        else None
                    ),
                )
            )
        return _snapshot(record)

    async def get_ready_snapshot(self, profile_id: UUID) -> ModelProfileSnapshot:
        record = await self._get_record(profile_id)
        if record.status != "ready":
            raise PipelineError(
                ErrorCode.MODEL_PROFILE_UNAVAILABLE,
                "model_profile",
                "selected model profile is not ready",
                retryable=False,
            )
        if not await asyncio.to_thread(self._matches_library_hashes, record):
            await self._mark_unavailable(record)
            raise PipelineError(
                ErrorCode.MODEL_PROFILE_UNAVAILABLE,
                "model_profile",
                "selected model profile is missing or corrupt",
                retryable=False,
            )
        return _snapshot(record)

    async def activate(self, profile_id: UUID) -> ModelProfileView:
        snapshot = await self.get_ready_snapshot(profile_id)
        async with self._database.write_session() as session:
            await session.execute(
                sqlite_insert(project_settings)
                .values(key=_ACTIVE_PROFILE_SETTING, value=str(profile_id))
                .on_conflict_do_update(
                    index_elements=[project_settings.c.key], set_={"value": str(profile_id)}
                )
            )
        return (await self._get_record(snapshot.profile_id)).to_view(active=True)

    async def resolve_active_snapshot(self) -> ModelProfileSnapshot:
        async with self._database.read_session() as session:
            profile_id = (
                await session.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == _ACTIVE_PROFILE_SETTING
                    )
                )
            ).scalar_one_or_none()
        if profile_id is None:
            raise PipelineError(
                ErrorCode.MODEL_PROFILE_UNAVAILABLE,
                "model_profile",
                "no active GPT-SoVITS model profile is configured",
                retryable=False,
            )
        return await self.get_ready_snapshot(UUID(str(profile_id)))

    async def list(self) -> list[ModelProfileView]:
        async with self._database.read_session() as session:
            active_value = (
                await session.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == _ACTIVE_PROFILE_SETTING
                    )
                )
            ).scalar_one_or_none()
            rows = (
                await session.execute(
                    select(model_profiles).order_by(model_profiles.c.created_at_utc)
                )
            ).mappings()
            records = [_record_from_row(dict(row)) for row in rows]
        return [record.to_view(active=str(record.profile_id) == active_value) for record in records]

    async def get_view(self, profile_id: UUID) -> ModelProfileView:
        record = await self._get_record(profile_id)
        async with self._database.read_session() as session:
            active_value = (
                await session.execute(
                    select(project_settings.c.value).where(
                        project_settings.c.key == _ACTIVE_PROFILE_SETTING
                    )
                )
            ).scalar_one_or_none()
        return record.to_view(active=str(profile_id) == active_value)

    async def _get_record(self, profile_id: UUID) -> ModelProfileRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(model_profiles).where(model_profiles.c.profile_id == str(profile_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PipelineError(
                ErrorCode.MODEL_PROFILE_NOT_FOUND,
                "model_profile",
                "model profile was not found",
                retryable=False,
            )
        return _record_from_row(dict(row))

    async def _mark_unavailable(self, record: ModelProfileRecord) -> None:
        status = "missing"
        async with self._database.write_session() as session:
            await session.execute(
                update(model_profiles)
                .where(model_profiles.c.profile_id == str(record.profile_id))
                .values(status=status)
            )

    def _matches_library_hashes(self, record: ModelProfileRecord) -> bool:
        try:
            gpt = _resolve_library_path(self._models_root, record.gpt_relative_path)
            sovits = _resolve_library_path(self._models_root, record.sovits_relative_path)
            return (
                sha256_file(gpt) == record.gpt_sha256
                and sha256_file(sovits) == record.sovits_sha256
            )
        except (OSError, ValueError):
            return False


def _snapshot(record: ModelProfileRecord) -> ModelProfileSnapshot:
    return ModelProfileSnapshot(
        profile_id=record.profile_id,
        display_name=record.display_name,
        gpt_relative_path=record.gpt_relative_path,
        sovits_relative_path=record.sovits_relative_path,
        gpt_sha256=record.gpt_sha256,
        sovits_sha256=record.sovits_sha256,
    )


def _record_from_row(row: dict[str, object]) -> ModelProfileRecord:
    archived_at = row["archived_at_utc"]
    return ModelProfileRecord(
        profile_id=UUID(str(row["profile_id"])),
        display_name=str(row["display_name"]),
        source_kind=cast(Literal["base", "imported"], str(row["source_kind"])),
        declared_family=str(row["declared_family"]) if row["declared_family"] is not None else None,
        relative_directory=PurePosixPath(str(row["relative_directory"])),
        gpt_relative_path=PurePosixPath(str(row["gpt_relative_path"])),
        sovits_relative_path=PurePosixPath(str(row["sovits_relative_path"])),
        gpt_sha256=str(row["gpt_sha256"]),
        sovits_sha256=str(row["sovits_sha256"]),
        gpt_size_bytes=int(str(row["gpt_size_bytes"])),
        sovits_size_bytes=int(str(row["sovits_size_bytes"])),
        status=cast(ModelProfileStatus, str(row["status"])),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        archived_at_utc=datetime.fromisoformat(str(archived_at))
        if archived_at is not None
        else None,
    )


def _resolve_library_path(root: Path, relative_path: PurePosixPath) -> Path:
    resolved = (root / relative_path).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("profile path escapes the model library") from exc
    return resolved
