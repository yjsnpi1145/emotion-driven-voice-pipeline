from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.director_reference_pool import PoolReferenceSpec
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import DirectorReferencePoolEntry
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import director_reference_pool_entries


class ReferencePoolStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def begin_attempt(
        self,
        *,
        family_key: str,
        base_voice_sha256: str,
        spec: PoolReferenceSpec,
        engine_fingerprint: dict[str, object],
        output_spec: dict[str, object],
        degraded_from: str | None = None,
    ) -> tuple[DirectorReferencePoolEntry, bool]:
        entry_id = uuid4()
        now = _now()
        async with self._database.write_session() as session:
            result = await session.execute(
                insert(director_reference_pool_entries)
                .values(
                    entry_id=str(entry_id),
                    family_key=family_key,
                    revision=spec.revision,
                    attempt=spec.attempt,
                    status="building",
                    base_voice_sha256=base_voice_sha256,
                    emotion_bucket=spec.bucket,
                    template_version=spec.template_version,
                    prompt_text=spec.prompt_text,
                    emotion_vector_json=_json(list(spec.emotion_vector)),
                    seed=spec.seed,
                    engine_fingerprint_json=_json(engine_fingerprint),
                    output_spec_json=_json(output_spec),
                    degraded_from=degraded_from,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
                .prefix_with("OR IGNORE")
            )
            claimed = cast(CursorResult[Any], result).rowcount == 1
            row = (
                (
                    await session.execute(
                        select(director_reference_pool_entries).where(
                            director_reference_pool_entries.c.family_key == family_key,
                            director_reference_pool_entries.c.revision == spec.revision,
                            director_reference_pool_entries.c.attempt == spec.attempt,
                        )
                    )
                )
                .mappings()
                .one()
            )
        return _entry(dict(row)), claimed

    async def get(self, entry_id: UUID) -> DirectorReferencePoolEntry:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_reference_pool_entries).where(
                            director_reference_pool_entries.c.entry_id == str(entry_id)
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown Director reference pool entry: {entry_id}")
        return _entry(dict(row))

    async def latest_ready(self, family_key: str) -> DirectorReferencePoolEntry | None:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(director_reference_pool_entries)
                        .where(
                            director_reference_pool_entries.c.family_key == family_key,
                            director_reference_pool_entries.c.status == "ready",
                        )
                        .order_by(
                            director_reference_pool_entries.c.revision.desc(),
                            director_reference_pool_entries.c.attempt.desc(),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _entry(dict(row)) if row is not None else None

    async def next_revision(self, family_key: str) -> int:
        async with self._database.read_session() as session:
            maximum = (
                await session.execute(
                    select(func.max(director_reference_pool_entries.c.revision)).where(
                        director_reference_pool_entries.c.family_key == family_key
                    )
                )
            ).scalar_one_or_none()
        return 0 if maximum is None else int(maximum) + 1

    async def mark_ready(
        self,
        entry_id: UUID,
        *,
        reference_job_id: UUID,
        reference_version_id: UUID,
        blob_sha256: str,
        quality_result: dict[str, object],
    ) -> DirectorReferencePoolEntry:
        await self._finish(
            entry_id,
            status="ready",
            reference_job_id=str(reference_job_id),
            reference_version_id=str(reference_version_id),
            blob_sha256=blob_sha256,
            quality_result_json=_json(quality_result),
            error_json=None,
        )
        return await self.get(entry_id)

    async def mark_failed(
        self,
        entry_id: UUID,
        *,
        reference_job_id: UUID | None,
        error: dict[str, object],
    ) -> DirectorReferencePoolEntry:
        await self._finish(
            entry_id,
            status="failed",
            reference_job_id=(str(reference_job_id) if reference_job_id else None),
            error_json=_json(error),
        )
        return await self.get(entry_id)

    async def _finish(self, entry_id: UUID, *, status: str, **values: object) -> None:
        async with self._database.write_session() as session:
            result = await session.execute(
                update(director_reference_pool_entries)
                .where(director_reference_pool_entries.c.entry_id == str(entry_id))
                .where(director_reference_pool_entries.c.status == "building")
                .values(status=status, updated_at_utc=_now(), **values)
            )
            if cast(CursorResult[Any], result).rowcount != 1:
                raise PipelineError(
                    ErrorCode.VERSION_CONFLICT,
                    "director_reference_pool",
                    "reference pool attempt is no longer building",
                    retryable=False,
                    details={"entry_id": str(entry_id)},
                )


def _entry(row: dict[str, Any]) -> DirectorReferencePoolEntry:
    return DirectorReferencePoolEntry(
        entry_id=UUID(str(row["entry_id"])),
        family_key=str(row["family_key"]),
        revision=int(row["revision"]),
        attempt=int(row["attempt"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        base_voice_sha256=str(row["base_voice_sha256"]),
        emotion_bucket=str(row["emotion_bucket"]),  # type: ignore[arg-type]
        template_version=int(row["template_version"]),
        prompt_text=str(row["prompt_text"]),
        emotion_vector=tuple(json.loads(str(row["emotion_vector_json"]))),
        seed=int(row["seed"]),
        engine_fingerprint=json.loads(str(row["engine_fingerprint_json"])),
        output_spec=json.loads(str(row["output_spec_json"])),
        reference_job_id=(
            UUID(str(row["reference_job_id"])) if row.get("reference_job_id") else None
        ),
        reference_version_id=(
            UUID(str(row["reference_version_id"]))
            if row.get("reference_version_id")
            else None
        ),
        blob_sha256=(str(row["blob_sha256"]) if row.get("blob_sha256") else None),
        quality_result=(
            json.loads(str(row["quality_result_json"]))
            if row.get("quality_result_json")
            else None
        ),
        error=json.loads(str(row["error_json"])) if row.get("error_json") else None,
        degraded_from=(str(row["degraded_from"]) if row.get("degraded_from") else None),  # type: ignore[arg-type]
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
        updated_at_utc=datetime.fromisoformat(str(row["updated_at_utc"])),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
