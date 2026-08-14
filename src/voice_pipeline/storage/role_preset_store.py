from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import RolePresetRecord
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import role_presets


class RolePresetStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def insert(self, record: RolePresetRecord) -> RolePresetRecord:
        async with self._database.write_session() as session:
            await session.execute(
                insert(role_presets).values(
                    preset_id=str(record.preset_id),
                    name=record.name,
                    base_voice_relative_path=record.base_voice_relative_path,
                    base_voice_sha256=record.base_voice_sha256,
                    byte_size=record.byte_size,
                    duration_seconds=record.duration_seconds,
                    sample_rate=record.sample_rate,
                    channels=record.channels,
                    model_profile_id=str(record.model_profile_id),
                    default_speed=record.default_speed,
                    status=record.status,
                    revision=record.revision,
                    created_at_utc=record.created_at_utc.isoformat(),
                    updated_at_utc=record.updated_at_utc.isoformat(),
                )
            )
        return await self.get(record.preset_id)

    async def get(self, preset_id: UUID) -> RolePresetRecord:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(role_presets).where(role_presets.c.preset_id == str(preset_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown role preset: {preset_id}")
        return _record(dict(row))

    async def list(self, *, include_archived: bool = False) -> list[RolePresetRecord]:
        statement = select(role_presets).order_by(
            role_presets.c.name, role_presets.c.created_at_utc
        )
        if not include_archived:
            statement = statement.where(role_presets.c.status != "archived")
        async with self._database.read_session() as session:
            rows = (await session.execute(statement)).mappings().all()
        return [_record(dict(row)) for row in rows]

    async def update_status(self, preset_id: UUID, status: str) -> RolePresetRecord:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(role_presets)
                .where(role_presets.c.preset_id == str(preset_id))
                .values(status=status, revision=role_presets.c.revision + 1)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise KeyError(f"unknown role preset: {preset_id}")
        return await self.get(preset_id)

    async def patch(
        self,
        preset_id: UUID,
        *,
        expected_revision: int,
        name: str | None,
        model_profile_id: UUID | None,
        default_speed: float | None,
    ) -> RolePresetRecord:
        values: dict[str, object] = {"revision": role_presets.c.revision + 1}
        if name is not None:
            values["name"] = name
        if model_profile_id is not None:
            values["model_profile_id"] = str(model_profile_id)
        if default_speed is not None:
            values["default_speed"] = default_speed
        async with self._database.write_session() as session:
            result = await session.execute(
                update(role_presets)
                .where(role_presets.c.preset_id == str(preset_id))
                .where(role_presets.c.revision == expected_revision)
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                exists = (
                    await session.execute(
                        select(role_presets.c.preset_id).where(
                            role_presets.c.preset_id == str(preset_id)
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise KeyError(f"unknown role preset: {preset_id}")
                raise PipelineError(
                    ErrorCode.VERSION_CONFLICT,
                    "role_preset",
                    "role preset changed; refresh before saving",
                    retryable=False,
                )
        return await self.get(preset_id)


def _record(row: dict[str, object]) -> RolePresetRecord:
    return RolePresetRecord(
        preset_id=UUID(str(row["preset_id"])),
        name=str(row["name"]),
        base_voice_relative_path=str(row["base_voice_relative_path"]),
        base_voice_sha256=str(row["base_voice_sha256"]),
        byte_size=int(str(row["byte_size"])),
        duration_seconds=float(str(row["duration_seconds"])),
        sample_rate=int(str(row["sample_rate"])),
        channels=int(str(row["channels"])),
        model_profile_id=UUID(str(row["model_profile_id"])),
        default_speed=float(str(row["default_speed"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        revision=int(str(row["revision"])),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )
