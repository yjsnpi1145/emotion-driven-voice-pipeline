from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.job_store import SqliteJobStore


async def _open_store(tmp_path: Path) -> tuple[Database, SqliteJobStore]:
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
    return database, SqliteJobStore(database, jobs_root=runtime / "jobs")


@pytest.mark.asyncio
async def test_job_survives_database_reopen(tmp_path: Path) -> None:
    first_db, first_store = await _open_store(tmp_path)
    context = await first_store.create(
        request_id=uuid4(),
        kind="reference",
        request_snapshot={"reference_text": "中文"},
    )
    await first_db.close()

    second_db, second_store = await _open_store(tmp_path)
    try:
        record = await second_store.get(context.job_id)
        assert record.status == "queued"
        assert record.request_snapshot == {"reference_text": "中文"}
        assert record.request_snapshot_sha256 == second_store.canonical_sha(record.request_snapshot)
    finally:
        await second_db.close()


@pytest.mark.asyncio
async def test_same_request_id_creates_two_distinct_jobs(tmp_path: Path) -> None:
    database, store = await _open_store(tmp_path)
    try:
        request_id = uuid4()
        first = await store.create(request_id=request_id, kind="gsv", request_snapshot={})
        second = await store.create(request_id=request_id, kind="gsv", request_snapshot={})
        assert first.job_id != second.job_id
        assert (await store.get(first.job_id)).request_id == request_id
        assert (await store.get(second.job_id)).request_id == request_id
    finally:
        await database.close()
