from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.models.chapter import ChapterSynthesisRequest
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.segment_store import SegmentStore


def _settings(tmp_path: Path) -> StorageSettings:
    runtime = tmp_path / "runtime"
    return StorageSettings(
        database_path=runtime / "state" / "pipeline.sqlite3",
        artifact_root=runtime / "artifacts",
        control_lock_path=runtime / "state" / "control.lock",
    )


@pytest.mark.asyncio
async def test_chapter_store_materializes_contiguous_directed_segments(tmp_path: Path) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        source = "第一句。第二句。"
        request = ChapterSynthesisRequest(
            request_id=uuid4(),
            title="chapter",
            source_text=source,
            target_language="ja",
            base_voice_path=tmp_path / "voice.wav",
            model_profile_id=uuid4(),
        )
        plan = await FakeDirector().create_plan(source_text=source, target_language="ja")
        store = ChapterStore(database, SegmentStore(database))

        run = await store.create_queued(
            request=request,
            director_plan=plan,
            model_profile_snapshot={"profile_id": str(request.model_profile_id)},
            base_voice_sha256=hashlib.sha256(b"voice").hexdigest(),
        )

        assert run.status == "queued"
        assert [item.source_text for item in await store.list_segments(run.run_id)] == [
            "第一句。",
            "第二句。",
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_chapter_store_recovery_marks_running_run_interrupted(tmp_path: Path) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        source = "一句。"
        request = ChapterSynthesisRequest(
            request_id=uuid4(),
            title="chapter",
            source_text=source,
            target_language="en",
            base_voice_path=tmp_path / "voice.wav",
            model_profile_id=uuid4(),
        )
        plan = await FakeDirector().create_plan(source_text=source, target_language="en")
        store = ChapterStore(database, SegmentStore(database))
        run = await store.create_queued(
            request=request,
            director_plan=plan,
            model_profile_snapshot={"profile_id": str(request.model_profile_id)},
            base_voice_sha256="a" * 64,
        )

        await store.mark_running(run.run_id)

        assert await store.mark_interrupted_running() == (run.run_id,)
        assert (await store.get(run.run_id)).status == "interrupted"
        assert len(await store.list_segments(run.run_id)) == 1
    finally:
        await database.close()
