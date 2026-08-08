from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import (
    ActivateVersionRequest,
    ActivationOutcome,
    ArtifactState,
    ArtifactType,
    ArtifactVersionRecord,
    ArtifactVersionView,
    JsonValue,
    PersistentJobRecord,
    VersionCommitResult,
)
from voice_pipeline.models.schemas import ReferenceBinding
from voice_pipeline.modules.quality.models import QualityReport
from voice_pipeline.storage.artifact_store import PublishedBlob
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import (
    artifact_blobs,
    artifact_version_state,
    artifact_versions,
    generation_jobs,
    job_artifacts,
    segments,
)


class VersionStore:
    """Immutable version metadata plus explicit, compare-and-set activation."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_version(
        self,
        *,
        segment_id: UUID,
        artifact_type: ArtifactType,
        source_job_id: UUID,
        blob: PublishedBlob,
        input_snapshot: dict[str, JsonValue],
        model_fingerprint: dict[str, JsonValue],
        quality_result: dict[str, JsonValue],
        ref_version_id: UUID | None = None,
        ref_content_sha256: str | None = None,
    ) -> ArtifactVersionView:
        if artifact_type == "gsv" and (ref_version_id is None or ref_content_sha256 is None):
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "versions",
                "GSV version requires the frozen reference version and hash",
                retryable=False,
            )
        version_id = uuid4()
        now = _now()
        input_json = _canonical_json(input_snapshot)
        fingerprint_json = _canonical_json(model_fingerprint)
        quality_json = _canonical_json(quality_result)
        async with self._database.write_session() as session:
            segment_exists = (
                await session.execute(
                    select(segments.c.segment_id).where(segments.c.segment_id == str(segment_id))
                )
            ).scalar_one_or_none()
            if segment_exists is None:
                raise KeyError(f"unknown segment: {segment_id}")
            await session.execute(
                insert(artifact_blobs)
                .values(
                    content_sha256=blob.content_sha256,
                    relative_path=blob.relative_path.as_posix(),
                    byte_size=blob.byte_size,
                    frames=blob.audio.frames,
                    sample_rate=blob.audio.sample_rate,
                    channels=blob.audio.channels,
                    duration_seconds=blob.audio.duration_seconds,
                    rms_dbfs=blob.audio.rms_dbfs,
                    peak_dbfs=blob.audio.peak_dbfs,
                    lifecycle_state="ready",
                    created_at_utc=now,
                    checked_at_utc=now,
                )
                .prefix_with("OR IGNORE")
            )
            display_ordinal = (
                int(
                    (
                        await session.execute(
                            select(
                                func.coalesce(func.max(artifact_versions.c.display_ordinal), -1)
                            ).where(
                                artifact_versions.c.segment_id == str(segment_id),
                                artifact_versions.c.artifact_type == artifact_type,
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            manifest_relative_path = f"manifests/versions/{version_id}.json"
            await session.execute(
                insert(artifact_versions).values(
                    version_id=str(version_id),
                    segment_id=str(segment_id),
                    artifact_type=artifact_type,
                    display_ordinal=display_ordinal,
                    source_job_id=str(source_job_id),
                    blob_sha256=blob.content_sha256,
                    manifest_relative_path=manifest_relative_path,
                    ref_version_id=str(ref_version_id) if ref_version_id is not None else None,
                    ref_content_sha256=ref_content_sha256,
                    input_snapshot_json=input_json,
                    input_snapshot_sha256=_sha(input_json),
                    model_fingerprint_json=fingerprint_json,
                    model_fingerprint_sha256=_sha(fingerprint_json),
                    model_profile_snapshot_json=None,
                    quality_profile_version="0" * 64,
                    quality_result_json=quality_json,
                    complete_cache_key=None,
                    created_at_utc=now,
                )
            )
            await session.execute(
                insert(artifact_version_state).values(
                    version_id=str(version_id),
                    state="ready",
                    diagnostic_json="{}",
                    checked_at_utc=now,
                )
            )
            await session.execute(
                insert(job_artifacts).values(
                    job_id=str(source_job_id),
                    version_id=str(version_id),
                    role=artifact_type,
                    stage_index=0,
                )
            )
        return await self.get_version(version_id)

    async def list_versions(
        self, segment_id: UUID, *, ready_only: bool = True
    ) -> list[ArtifactVersionView]:
        async with self._database.read_session() as session:
            statement = (
                select(
                    artifact_versions,
                    artifact_version_state.c.state,
                    artifact_version_state.c.diagnostic_json,
                    artifact_blobs.c.relative_path,
                )
                .join(
                    artifact_version_state,
                    artifact_versions.c.version_id == artifact_version_state.c.version_id,
                )
                .join(
                    artifact_blobs,
                    artifact_versions.c.blob_sha256 == artifact_blobs.c.content_sha256,
                )
                .where(artifact_versions.c.segment_id == str(segment_id))
                .order_by(
                    artifact_versions.c.created_at_utc.desc(), artifact_versions.c.version_id.desc()
                )
            )
            if ready_only:
                statement = statement.where(artifact_version_state.c.state == "ready")
            rows = (await session.execute(statement)).mappings().all()
        return [_view(dict(row)) for row in rows]

    async def get_version(self, version_id: UUID) -> ArtifactVersionView:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(
                            artifact_versions,
                            artifact_version_state.c.state,
                            artifact_version_state.c.diagnostic_json,
                            artifact_blobs.c.relative_path,
                        )
                        .join(
                            artifact_version_state,
                            artifact_versions.c.version_id == artifact_version_state.c.version_id,
                        )
                        .join(
                            artifact_blobs,
                            artifact_versions.c.blob_sha256 == artifact_blobs.c.content_sha256,
                        )
                        .where(artifact_versions.c.version_id == str(version_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise KeyError(f"unknown version: {version_id}")
        return _view(dict(row))

    async def activate(
        self,
        segment_id: UUID,
        version_id: UUID,
        request: ActivateVersionRequest,
    ) -> tuple[UUID | None, UUID | None]:
        version = await self.get_version(version_id)
        if version.segment_id != segment_id:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "versions",
                "version belongs to a different segment",
                retryable=False,
            )
        if version.state != "ready":
            raise PipelineError(
                ErrorCode.VERSION_NOT_READY,
                "versions",
                "only a ready version can be activated",
                retryable=False,
            )
        pointer = (
            "active_ref_version_id"
            if version.artifact_type == "reference"
            else "active_gsv_version_id"
        )
        async with self._database.write_session() as session:
            result = await session.execute(
                update(segments)
                .where(segments.c.segment_id == str(segment_id))
                .where(segments.c.selection_revision == request.expected_selection_revision)
                .values(
                    **{
                        pointer: str(version_id),
                        "selection_revision": segments.c.selection_revision + 1,
                        "revision": segments.c.revision + 1,
                        "updated_at_utc": _now(),
                    }
                )
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
                    "versions",
                    "segment selection revision has changed",
                    retryable=False,
                )
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(
                            segments.c.active_ref_version_id, segments.c.active_gsv_version_id
                        ).where(segments.c.segment_id == str(segment_id))
                    )
                )
                .mappings()
                .one()
            )
        return (
            UUID(str(row["active_ref_version_id"])) if row["active_ref_version_id"] else None,
            UUID(str(row["active_gsv_version_id"])) if row["active_gsv_version_id"] else None,
        )


class VersionCommitService:
    """Commits a segment-bound execution, its version and its terminal job atomically."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def commit_reference(
        self,
        *,
        job: PersistentJobRecord,
        blob: PublishedBlob,
        reference: ReferenceBinding,
        quality: QualityReport,
        activate_on_success: bool,
        result: dict[str, JsonValue],
    ) -> VersionCommitResult:
        return await self._commit(
            job=job,
            artifact_type="reference",
            blob=blob,
            input_snapshot={
                **job.request_snapshot,
                "base_voice_sha256": reference.base_voice_sha256,
            },
            model_fingerprint=reference.engine_fingerprint.model_dump(mode="json"),
            quality=quality,
            reference_version_id=None,
            reference_content_sha256=None,
            activate_on_success=activate_on_success,
            result=result,
        )

    async def commit_gsv(
        self,
        *,
        job: PersistentJobRecord,
        blob: PublishedBlob,
        reference_version: ArtifactVersionView,
        quality: QualityReport,
        model_fingerprint: dict[str, JsonValue],
        activate_on_success: bool,
        result: dict[str, JsonValue],
    ) -> VersionCommitResult:
        return await self._commit(
            job=job,
            artifact_type="gsv",
            blob=blob,
            input_snapshot=job.request_snapshot,
            model_fingerprint=model_fingerprint,
            quality=quality,
            reference_version_id=reference_version.version_id,
            reference_content_sha256=reference_version.blob_sha256,
            activate_on_success=activate_on_success,
            result=result,
        )

    async def _commit(
        self,
        *,
        job: PersistentJobRecord,
        artifact_type: ArtifactType,
        blob: PublishedBlob,
        input_snapshot: dict[str, JsonValue],
        model_fingerprint: dict[str, JsonValue],
        quality: QualityReport,
        reference_version_id: UUID | None,
        reference_content_sha256: str | None,
        activate_on_success: bool,
        result: dict[str, JsonValue],
    ) -> VersionCommitResult:
        snapshot = job.task_snapshot
        if snapshot is None:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "versions",
                "version commit requires a frozen segment snapshot",
                retryable=False,
            )
        if artifact_type == "gsv" and (
            reference_version_id is None or reference_content_sha256 is None
        ):
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "versions",
                "GSV version requires a frozen reference version",
                retryable=False,
            )
        version_id = uuid4()
        now = _now()
        input_json = _canonical_json(input_snapshot)
        fingerprint_json = _canonical_json(model_fingerprint)
        quality_json = _canonical_json(quality.model_dump(mode="json"))
        async with self._database.write_session() as session:
            exists = (
                await session.execute(
                    select(segments.c.segment_id).where(
                        segments.c.segment_id == str(snapshot.segment_id)
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise KeyError(f"unknown segment: {snapshot.segment_id}")
            await session.execute(
                insert(artifact_blobs)
                .values(
                    content_sha256=blob.content_sha256,
                    relative_path=blob.relative_path.as_posix(),
                    byte_size=blob.byte_size,
                    frames=blob.audio.frames,
                    sample_rate=blob.audio.sample_rate,
                    channels=blob.audio.channels,
                    duration_seconds=blob.audio.duration_seconds,
                    rms_dbfs=blob.audio.rms_dbfs,
                    peak_dbfs=blob.audio.peak_dbfs,
                    lifecycle_state="ready",
                    created_at_utc=now,
                    checked_at_utc=now,
                )
                .prefix_with("OR IGNORE")
            )
            ordinal = (
                int(
                    (
                        await session.execute(
                            select(
                                func.coalesce(func.max(artifact_versions.c.display_ordinal), -1)
                            ).where(
                                artifact_versions.c.segment_id == str(snapshot.segment_id),
                                artifact_versions.c.artifact_type == artifact_type,
                            )
                        )
                    ).scalar_one()
                )
                + 1
            )
            await session.execute(
                insert(artifact_versions).values(
                    version_id=str(version_id),
                    segment_id=str(snapshot.segment_id),
                    artifact_type=artifact_type,
                    display_ordinal=ordinal,
                    source_job_id=str(job.job_id),
                    blob_sha256=blob.content_sha256,
                    manifest_relative_path=f"manifests/versions/{version_id}.json",
                    ref_version_id=str(reference_version_id) if reference_version_id else None,
                    ref_content_sha256=reference_content_sha256,
                    input_snapshot_json=input_json,
                    input_snapshot_sha256=_sha(input_json),
                    model_fingerprint_json=fingerprint_json,
                    model_fingerprint_sha256=_sha(fingerprint_json),
                    model_profile_snapshot_json=None,
                    quality_profile_version=quality.policy_fingerprint,
                    quality_result_json=quality_json,
                    complete_cache_key=None,
                    created_at_utc=now,
                )
            )
            await session.execute(
                insert(artifact_version_state).values(
                    version_id=str(version_id),
                    state="ready",
                    diagnostic_json="{}",
                    checked_at_utc=now,
                )
            )
            await session.execute(
                insert(job_artifacts).values(
                    job_id=str(job.job_id),
                    version_id=str(version_id),
                    role=artifact_type,
                    stage_index=0,
                )
            )
            cancellation_requested = (
                await session.execute(
                    select(generation_jobs.c.cancel_requested_at_utc).where(
                        generation_jobs.c.job_id == str(job.job_id)
                    )
                )
            ).scalar_one()
            if cancellation_requested is not None:
                outcome: ActivationOutcome = "cancelled"
                terminal_status = "cancelled"
            else:
                outcome = await self._activate_if_current(
                    session=session,
                    snapshot=snapshot,
                    artifact_type=artifact_type,
                    version_id=version_id,
                    activate_on_success=activate_on_success,
                    now=now,
                )
                terminal_status = "succeeded"
            terminal = await session.execute(
                update(generation_jobs)
                .where(generation_jobs.c.job_id == str(job.job_id))
                .where(generation_jobs.c.status == "running")
                .values(
                    status=terminal_status,
                    stage=terminal_status,
                    result_json=_canonical_json(result) if terminal_status == "succeeded" else None,
                    error_json=None,
                    activation_outcome=outcome,
                    finished_at_utc=now,
                )
            )
            if cast(CursorResult[Any], terminal).rowcount != 1:
                raise PipelineError(
                    ErrorCode.JOB_STATE_CONFLICT,
                    "versions",
                    "job no longer accepts a version commit",
                    retryable=False,
                )
        version = await VersionStore(self._database).get_version(version_id)
        return VersionCommitResult(
            version=version,
            activation_outcome=outcome,
            terminal_status=terminal_status,  # type: ignore[arg-type]
        )

    async def _activate_if_current(
        self,
        *,
        session: Any,
        snapshot: Any,
        artifact_type: ArtifactType,
        version_id: UUID,
        activate_on_success: bool,
        now: str,
    ) -> ActivationOutcome:
        if not activate_on_success:
            return "history_only"
        pointer = (
            "active_ref_version_id" if artifact_type == "reference" else "active_gsv_version_id"
        )
        statement = (
            update(segments)
            .where(segments.c.segment_id == str(snapshot.segment_id))
            .where(segments.c.selection_revision == snapshot.selection_revision)
        )
        if artifact_type == "reference":
            statement = statement.where(
                segments.c.ref_draft_revision == snapshot.ref_draft_revision
            )
        else:
            statement = statement.where(
                segments.c.gsv_draft_revision == snapshot.gsv_draft_revision
            ).where(segments.c.active_ref_version_id == str(snapshot.active_ref_version_id))
        changed = await session.execute(
            statement.values(
                **{
                    pointer: str(version_id),
                    "selection_revision": segments.c.selection_revision + 1,
                    "revision": segments.c.revision + 1,
                    "updated_at_utc": now,
                }
            )
        )
        return "activated" if cast(CursorResult[Any], changed).rowcount == 1 else "history_only"


def _view(row: dict[str, Any]) -> ArtifactVersionView:
    record = ArtifactVersionRecord(
        version_id=UUID(str(row["version_id"])),
        segment_id=UUID(str(row["segment_id"])) if row["segment_id"] else None,
        artifact_type=cast(ArtifactType, str(row["artifact_type"])),
        source_job_id=UUID(str(row["source_job_id"])),
        blob_sha256=str(row["blob_sha256"]),
        manifest_relative_path=str(row["manifest_relative_path"]),
        ref_version_id=UUID(str(row["ref_version_id"])) if row["ref_version_id"] else None,
        ref_content_sha256=str(row["ref_content_sha256"]) if row["ref_content_sha256"] else None,
        input_snapshot=cast(dict[str, JsonValue], json.loads(str(row["input_snapshot_json"]))),
        model_fingerprint=cast(
            dict[str, JsonValue], json.loads(str(row["model_fingerprint_json"]))
        ),
        quality_result=cast(dict[str, JsonValue], json.loads(str(row["quality_result_json"]))),
        state=cast(ArtifactState, str(row["state"])),
        created_at_utc=datetime.fromisoformat(str(row["created_at_utc"])),
    )
    return ArtifactVersionView(
        **record.model_dump(),
        blob_relative_path=Path(str(row["relative_path"])),
        diagnostic=cast(dict[str, JsonValue], json.loads(str(row["diagnostic_json"]))),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
