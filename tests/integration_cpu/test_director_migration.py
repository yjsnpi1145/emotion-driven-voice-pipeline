from __future__ import annotations

import sqlite3
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
        assert await database.alembic_revision() == "0006_director_preprocessing"
        async with database.read_session() as session:
            rows = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            names = {str(row[0]) for row in rows}
            utterance_rows = await session.execute(text("PRAGMA table_info(director_utterances)"))
            columns = {str(row[1]) for row in utterance_rows}
            project_rows = await session.execute(text("PRAGMA table_info(director_projects)"))
            project_columns = {str(row[1]) for row in project_rows}
        assert {
            "director_projects",
            "director_analysis_chunks",
            "director_roles",
            "director_utterances",
            "director_edit_events",
            "role_presets",
            "director_generations",
            "director_generation_items",
            "director_preprocess_paragraphs",
        } <= names
        assert "working_text" in columns
        assert {
            "preprocessing_mode",
            "structural_text",
            "preprocessed_text",
            "preprocess_revision",
        } <= project_columns
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_director_working_text_migration_backfills_existing_source(tmp_path: Path) -> None:
    settings = storage_settings(tmp_path)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES ('0004_director_mode')"
        )
        connection.execute(
            "CREATE TABLE director_utterances ("
            "utterance_id TEXT PRIMARY KEY, source_text TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE director_projects (project_id TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO director_utterances (utterance_id, source_text) VALUES (?, ?)",
            (str(uuid4()), " 原始切片。 "),
        )
        connection.commit()
    finally:
        connection.close()

    database = await Database.open(settings, instance_id=uuid4(), migrate=True)
    try:
        assert await database.alembic_revision() == "0006_director_preprocessing"
        async with database.read_session() as session:
            row = (
                await session.execute(
                    text("SELECT source_text, working_text FROM director_utterances")
                )
            ).one()
        assert tuple(row) == (" 原始切片。 ", " 原始切片。 ")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preprocessing_migration_does_not_convert_legacy_project_text(
    tmp_path: Path,
) -> None:
    settings = storage_settings(tmp_path)
    database = await Database.open(settings, instance_id=uuid4(), migrate=True)
    try:
        async with database.read_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT preprocessing_mode, structural_text, preprocessed_text, "
                        "preprocess_revision FROM director_projects LIMIT 1"
                    )
                )
            ).one_or_none()
        assert row is None
    finally:
        await database.close()
