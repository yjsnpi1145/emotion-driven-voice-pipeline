from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select

from tests.unit.conftest import write_tone
from voice_pipeline.core.config import StorageSettings
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.orm import (
    artifact_blobs,
    artifact_version_state,
    artifact_versions,
)
from voice_pipeline.storage.recovery import StorageRecovery


@pytest.mark.asyncio
async def test_recovery_removes_staging_quarantines_orphans_and_marks_missing_versions(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    database = await Database.open(
        StorageSettings(
            database_path=runtime / "state" / "pipeline.sqlite3",
            artifact_root=runtime / "artifacts",
            control_lock_path=runtime / "state" / "control.lock",
        ),
        instance_id=uuid4(),
        migrate=True,
    )
    store = ArtifactStore(runtime / "artifacts")
    jobs = SqliteJobStore(database, jobs_root=runtime / "jobs")
    try:
        source = tmp_path / "tone.wav"
        write_tone(source, seconds=4.0)
        valid = store.publish_blob(store.stage_audio(uuid4(), source))
        staging = store.root / "staging" / "crashed" / "partial.wav"
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b"partial")
        orphan_sha = "a" * 64
        orphan = store.blob_path(orphan_sha)
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")

        job_context = await jobs.create(request_id=uuid4(), kind="reference", request_snapshot={})
        assert await jobs.mark_running(job_context.job_id)
        assert await jobs.mark_succeeded(job_context.job_id, result={"ok": True})
        missing_sha = "b" * 64
        version_id = uuid4()
        now = datetime.now(UTC).isoformat()
        async with database.write_session() as session:
            await session.execute(
                insert(artifact_blobs).values(
                    content_sha256=valid.content_sha256,
                    relative_path=valid.relative_path.as_posix(),
                    byte_size=valid.byte_size,
                    frames=valid.audio.frames,
                    sample_rate=valid.audio.sample_rate,
                    channels=valid.audio.channels,
                    duration_seconds=valid.audio.duration_seconds,
                    rms_dbfs=valid.audio.rms_dbfs,
                    peak_dbfs=valid.audio.peak_dbfs,
                    lifecycle_state="ready",
                    created_at_utc=now,
                    checked_at_utc=now,
                )
            )
            await session.execute(
                insert(artifact_blobs).values(
                    content_sha256=missing_sha,
                    relative_path=f"blobs/sha256/bb/{missing_sha}.wav",
                    byte_size=1,
                    frames=1,
                    sample_rate=22050,
                    channels=1,
                    duration_seconds=1.0,
                    rms_dbfs=-20.0,
                    peak_dbfs=-10.0,
                    lifecycle_state="ready",
                    created_at_utc=now,
                    checked_at_utc=now,
                )
            )
            await session.execute(
                insert(artifact_versions).values(
                    version_id=str(version_id),
                    segment_id=None,
                    artifact_type="reference",
                    display_ordinal=None,
                    source_job_id=str(job_context.job_id),
                    blob_sha256=missing_sha,
                    manifest_relative_path="manifests/missing.json",
                    ref_version_id=None,
                    ref_content_sha256=None,
                    input_snapshot_json="{}",
                    input_snapshot_sha256="0" * 64,
                    model_fingerprint_json="{}",
                    model_fingerprint_sha256="0" * 64,
                    model_profile_snapshot_json=None,
                    quality_profile_version="0" * 64,
                    quality_result_json="{}",
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

        report = await StorageRecovery(database, store, orphan_grace_seconds=0).reconcile()

        assert staging in report.removed_partials
        assert orphan in report.quarantined_orphans
        assert valid.absolute_path.is_file()
        assert report.missing_versions == (version_id,)
        async with database.read_session() as session:
            state = (
                await session.execute(
                    select(artifact_version_state.c.state).where(
                        artifact_version_state.c.version_id == str(version_id)
                    )
                )
            ).scalar_one()
        assert state == "missing"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_clears_current_pointer_when_its_blob_is_missing(
    fake_settings, tmp_path: Path
) -> None:
    """Recovery never leaves a current pointer aimed at a non-ready version."""
    from tests.integration_cpu.conftest import write_tone
    from tests.integration_cpu.test_segment_bound_jobs import _create_segment, _wait
    from voice_pipeline.api.app import create_app

    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    first = create_app(fake_settings)
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            segment_id = await _create_segment(client)
            submitted = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "6a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                },
            )
            assert (await _wait(client, submitted.json()["job_id"]))["status"] == "succeeded"
            segment = (await client.get(f"/api/v1/segments/{segment_id}")).json()
            version = (
                await client.get(f"/api/v1/versions/{segment['active_ref_version_id']}")
            ).json()
            blob = first.state.plane.artifact_store.root / version["blob_relative_path"]
            blob.unlink()

    second = create_app(fake_settings)
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            repaired = await client.get(f"/api/v1/segments/{segment_id}")
            health = await client.get("/api/v1/health")

    assert repaired.json()["active_ref_version_id"] is None
    assert health.json()["storage"]["missing_ready_versions"] == 1
