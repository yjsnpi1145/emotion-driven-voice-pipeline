from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from voice_pipeline.models.schemas import StrictModel
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import (
    artifact_blobs,
    artifact_version_state,
    artifact_versions,
    segments,
    storage_meta,
)


class RecoveryReport(StrictModel):
    recovery_run_id: UUID
    removed_partials: tuple[Path, ...]
    quarantined_orphans: tuple[Path, ...]
    missing_versions: tuple[UUID, ...]
    corrupt_versions: tuple[UUID, ...]


class StorageRecovery:
    """Reconcile filesystem publication windows without choosing a new current."""

    def __init__(
        self,
        database: Database,
        store: ArtifactStore,
        *,
        orphan_grace_seconds: float = 60.0,
    ) -> None:
        self._database = database
        self._store = store
        self._orphan_grace_seconds = orphan_grace_seconds

    async def reconcile(self) -> RecoveryReport:
        recovery_run_id = uuid4()
        removed = self._remove_staging_files()
        expected_blobs = await self._expected_blob_hashes()
        quarantined = self._quarantine_old_orphans(recovery_run_id, expected_blobs)
        missing, corrupt = await self._verify_ready_versions()
        return RecoveryReport(
            recovery_run_id=recovery_run_id,
            removed_partials=tuple(removed),
            quarantined_orphans=tuple(quarantined),
            missing_versions=tuple(missing),
            corrupt_versions=tuple(corrupt),
        )

    def _remove_staging_files(self) -> list[Path]:
        staging = self._store.root / "staging"
        if not staging.exists():
            return []
        removed: list[Path] = []
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_symlink():
                path.unlink(missing_ok=True)
                removed.append(path)
            elif path.is_file():
                path.unlink(missing_ok=True)
                removed.append(path)
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
        return removed

    async def _expected_blob_hashes(self) -> set[str]:
        async with self._database.read_session() as session:
            values = (
                (await session.execute(select(artifact_blobs.c.content_sha256))).scalars().all()
            )
        return {str(value) for value in values}

    def _quarantine_old_orphans(
        self, recovery_run_id: UUID, expected_hashes: set[str]
    ) -> list[Path]:
        blobs = self._store.root / "blobs" / "sha256"
        if not blobs.exists():
            return []
        now = time.time()
        quarantined: list[Path] = []
        for path in blobs.rglob("*.wav"):
            content_sha = path.stem
            if content_sha in expected_hashes:
                continue
            try:
                old_enough = now - path.stat().st_mtime >= self._orphan_grace_seconds
            except OSError:
                continue
            if not old_enough:
                continue
            relative = path.relative_to(self._store.root)
            destination = self._store.root / "quarantine" / str(recovery_run_id) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            receipt = destination.with_suffix(destination.suffix + ".receipt.json")
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "recovery_run_id": str(recovery_run_id),
                        "original_path": str(path),
                        "quarantine_path": str(destination),
                        "reason": "published_blob_without_database_row",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            quarantined.append(path)
        return quarantined

    async def _verify_ready_versions(self) -> tuple[list[UUID], list[UUID]]:
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            artifact_versions.c.version_id,
                            artifact_versions.c.blob_sha256,
                            artifact_versions.c.artifact_type,
                            artifact_versions.c.manifest_relative_path,
                            artifact_blobs.c.relative_path,
                            artifact_version_state.c.state,
                        )
                        .join(
                            artifact_blobs,
                            artifact_versions.c.blob_sha256 == artifact_blobs.c.content_sha256,
                        )
                        .join(
                            artifact_version_state,
                            artifact_versions.c.version_id == artifact_version_state.c.version_id,
                        )
                        .where(artifact_version_state.c.state == "ready")
                    )
                )
                .mappings()
                .all()
            )
        missing: list[UUID] = []
        corrupt: list[UUID] = []
        now = datetime.now(UTC).isoformat()
        for row in rows:
            version_id = UUID(str(row["version_id"]))
            blob_path = (self._store.root / str(row["relative_path"])).resolve()
            expected_sha = str(row["blob_sha256"])
            state: str | None = None
            reason: str | None = None
            if not blob_path.is_file() or blob_path.is_symlink():
                state = "missing"
                reason = "canonical_blob_missing"
                missing.append(version_id)
            else:
                try:
                    if sha256_file(blob_path) != expected_sha:
                        state = "corrupt"
                        reason = "canonical_blob_corrupt"
                        corrupt.append(version_id)
                except OSError:
                    state = "missing"
                    reason = "canonical_blob_missing"
                    missing.append(version_id)
            if state is None:
                manifest_state = _verify_version_manifest(
                    root=self._store.root,
                    relative_path=str(row["manifest_relative_path"]),
                    version_id=version_id,
                    artifact_type=str(row["artifact_type"]),
                    blob_sha256=expected_sha,
                )
                if manifest_state is not None:
                    state, reason = manifest_state
                    (missing if state == "missing" else corrupt).append(version_id)
            if state is not None:
                async with self._database.write_session() as session:
                    await session.execute(
                        update(artifact_version_state)
                        .where(artifact_version_state.c.version_id == str(version_id))
                        .where(artifact_version_state.c.state == "ready")
                        .values(
                            state=state,
                            diagnostic_json=json.dumps(
                                {
                                    "reason": reason,
                                    "blob_sha256": expected_sha,
                                },
                                sort_keys=True,
                            ),
                            checked_at_utc=now,
                        )
                    )
                    ref_pointer = await session.execute(
                        update(segments)
                        .where(segments.c.active_ref_version_id == str(version_id))
                        .values(
                            active_ref_version_id=None,
                            selection_revision=segments.c.selection_revision + 1,
                            revision=segments.c.revision + 1,
                            updated_at_utc=now,
                        )
                    )
                    gsv_pointer = await session.execute(
                        update(segments)
                        .where(segments.c.active_gsv_version_id == str(version_id))
                        .values(
                            active_gsv_version_id=None,
                            selection_revision=segments.c.selection_revision + 1,
                            revision=segments.c.revision + 1,
                            updated_at_utc=now,
                        )
                    )
                    if (
                        cast(CursorResult[Any], ref_pointer).rowcount
                        or cast(CursorResult[Any], gsv_pointer).rowcount
                    ):
                        await session.execute(
                            update(storage_meta)
                            .where(storage_meta.c.singleton_id == 1)
                            .values(
                                protected_graph_revision=(
                                    storage_meta.c.protected_graph_revision + 1
                                )
                            )
                        )
        return missing, corrupt


def _verify_version_manifest(
    *,
    root: Path,
    relative_path: str,
    version_id: UUID,
    artifact_type: str,
    blob_sha256: str,
) -> tuple[str, str] | None:
    """Return an invalid state/reason, or ``None`` for a matching immutable manifest."""
    manifests = (root / "manifests").resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(manifests)
    except ValueError:
        return "corrupt", "version_manifest_path_outside_manifests"
    if not path.is_file() or path.is_symlink():
        return "missing", "version_manifest_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blob = payload["blob"]
        if (
            payload["version_id"] != str(version_id)
            or payload["artifact_type"] != artifact_type
            or blob["content_sha256"] != blob_sha256
        ):
            return "corrupt", "version_manifest_mismatch"
    except (OSError, TypeError, KeyError, json.JSONDecodeError):
        return "corrupt", "version_manifest_invalid_json"
    return None
