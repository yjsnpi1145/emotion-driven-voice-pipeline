from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.director_reference_pool import reference_spec
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.reference_pool_store import ReferencePoolStore


def _settings(tmp_path: Path) -> StorageSettings:
    runtime = tmp_path / "runtime"
    return StorageSettings(
        database_path=runtime / "state" / "pipeline.sqlite3",
        artifact_root=runtime / "artifacts",
        control_lock_path=runtime / "state" / "control.lock",
    )


@pytest.mark.asyncio
async def test_pool_store_claims_each_attempt_once_and_keeps_failures(tmp_path: Path) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        store = ReferencePoolStore(database)
        spec = reference_spec("surprise", revision=0, attempt=0)
        values = {
            "family_key": "a" * 64,
            "base_voice_sha256": "b" * 64,
            "spec": spec,
            "engine_fingerprint": {"engine": "indextts", "model_revision": "one"},
            "output_spec": {"sample_rate": 22050},
        }

        first, claimed = await store.begin_attempt(**values)
        duplicate, duplicate_claimed = await store.begin_attempt(**values)
        assert claimed is True
        assert duplicate_claimed is False
        assert duplicate.entry_id == first.entry_id

        failed = await store.mark_failed(
            first.entry_id,
            reference_job_id=uuid4(),
            error={"code": "QUALITY_VAD_FAILED", "message": "no speech"},
        )
        assert failed.status == "failed"
        assert failed.error == {"code": "QUALITY_VAD_FAILED", "message": "no speech"}
        assert await store.latest_ready("a" * 64) is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_pool_store_returns_latest_ready_revision(tmp_path: Path) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        store = ReferencePoolStore(database)
        ready = []
        for revision in (0, 1):
            entry, claimed = await store.begin_attempt(
                family_key="c" * 64,
                base_voice_sha256="d" * 64,
                spec=reference_spec("calm", revision=revision, attempt=0),
                engine_fingerprint={"engine": "indextts", "model_revision": "one"},
                output_spec={"sample_rate": 22050},
            )
            assert claimed
            entry = await store.mark_ready(
                entry.entry_id,
                reference_job_id=uuid4(),
                reference_version_id=uuid4(),
                blob_sha256=f"{revision + 1:x}" * 64,
                quality_result={"passed": True},
            )
            ready.append(entry)

        assert (await store.latest_ready("c" * 64)).entry_id == ready[1].entry_id  # type: ignore[union-attr]
        assert await store.next_revision("c" * 64) == 2
    finally:
        await database.close()
