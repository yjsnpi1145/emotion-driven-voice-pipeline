from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.storage.database import PACKAGED_HEAD, Database


def storage_settings(tmp_path: Path) -> StorageSettings:
    runtime = tmp_path / "runtime"
    return StorageSettings(
        database_path=runtime / "state" / "pipeline.sqlite3",
        artifact_root=runtime / "artifacts",
        control_lock_path=runtime / "state" / "control.lock",
    )


@pytest.mark.asyncio
async def test_empty_database_upgrades_to_packaged_head(tmp_path: Path) -> None:
    database = await Database.open(storage_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        assert await database.scalar_text("PRAGMA journal_mode") == "wal"
        assert await database.scalar_int("PRAGMA foreign_keys") == 1
        assert await database.alembic_revision() == PACKAGED_HEAD
        assert await database.quick_check_text() == "ok"
    finally:
        await database.close()
