from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.storage.database import Database


def storage_settings(tmp_path: Path) -> StorageSettings:
    runtime = tmp_path / "runtime"
    return StorageSettings(
        database_path=runtime / "state" / "pipeline.sqlite3",
        artifact_root=runtime / "artifacts",
        control_lock_path=runtime / "state" / "control.lock",
    )


@pytest.mark.asyncio
async def test_director_migration_creates_all_tables(tmp_path: Path) -> None:
    database = await Database.open(storage_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        assert await database.alembic_revision() == "0004_director_mode"
        async with database.read_session() as session:
            rows = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            names = {str(row[0]) for row in rows}
        assert {
            "director_projects",
            "director_analysis_chunks",
            "director_roles",
            "director_utterances",
            "director_edit_events",
            "role_presets",
            "director_generations",
            "director_generation_items",
        } <= names
    finally:
        await database.close()
