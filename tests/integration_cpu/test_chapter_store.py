from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.models.chapter import ChapterSynthesisRequest
from voice_pipeline.models.persistence import SegmentJobSnapshot
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.job_store import SqliteJobStore
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
async def test_chapter_store_keeps_source_translation_and_chinese_reference_separate(
    tmp_path: Path,
) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        source = "这是需要翻译的中文原文。"
        request = ChapterSynthesisRequest(
            request_id=uuid4(),
            title="translated chapter",
            source_text=source,
            target_language="ja",
            base_voice_path=tmp_path / "voice.wav",
            model_profile_id=uuid4(),
        )
        plan = DirectorPlan(
            source_text_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            segments=(
                DirectedSegment(
                    ordinal=0,
                    source_start=0,
                    source_end=len(source),
                    emotion_description="平静、克制",
                    emotion_vector=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
                    synthesis_text="これは翻訳が必要な中国語の原文です。",
                    ref_text_cn="这是需要翻译的中文原文。",
                    pause_after_ms=500,
                    speed_factor=1.0,
                    seed=1234,
                ),
            ),
        )
        store = ChapterStore(database, SegmentStore(database))

        run = await store.create_queued(
            request=request,
            director_plan=plan,
            model_profile_snapshot={"profile_id": str(request.model_profile_id)},
            base_voice_sha256="a" * 64,
        )

        segment = (await store.list_segments(run.run_id))[0]
        assert segment.source_text == source
        assert segment.synthesis_text == "これは翻訳が必要な中国語の原文です。"
        assert segment.ref_text_cn == "这是需要翻译的中文原文。"
        assert segment.target_language == "ja"
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


@pytest.mark.asyncio
async def test_chapter_progress_persists_job_ids_and_hides_private_snapshot(tmp_path: Path) -> None:
    database = await Database.open(_settings(tmp_path), instance_id=uuid4(), migrate=True)
    try:
        source = "第一句。"
        request = ChapterSynthesisRequest(
            request_id=uuid4(),
            title="chapter",
            source_text=source,
            target_language="en",
            base_voice_path=tmp_path / "private-voice.wav",
            model_profile_id=uuid4(),
        )
        segment_store = SegmentStore(database)
        store = ChapterStore(database, segment_store)
        plan = await FakeDirector().create_plan(source_text=source, target_language="en")
        run = await store.create_queued(
            request=request,
            director_plan=plan,
            model_profile_snapshot={"profile_id": str(request.model_profile_id)},
            base_voice_sha256="a" * 64,
        )
        segment = (await store.list_segments(run.run_id))[0]
        jobs = SqliteJobStore(database, jobs_root=tmp_path / "jobs")
        snapshot = SegmentJobSnapshot(
            task_id=segment.task_id,
            segment_id=segment.segment_id,
            ref_draft_revision=segment.ref_draft_revision,
            gsv_draft_revision=segment.gsv_draft_revision,
            selection_revision=segment.selection_revision,
            activate_on_success=True,
        )
        reference = await jobs.create(
            request_id=uuid4(),
            kind="reference",
            request_snapshot={"kind": "reference"},
            segment_snapshot=snapshot,
        )
        gsv = await jobs.create(
            request_id=uuid4(),
            kind="gsv",
            request_snapshot={"kind": "gsv"},
            segment_snapshot=snapshot,
        )

        await store.set_segment_job(run.run_id, segment.ordinal, "reference", reference.job_id)
        await store.set_segment_job(run.run_id, segment.ordinal, "gsv", gsv.job_id)

        progress = await store.progress(run.run_id)

        assert len(progress) == 1
        assert progress[0].ordinal == 0
        assert progress[0].segment_id == segment.segment_id
        assert progress[0].reference_job_status == "queued"
        assert progress[0].gsv_job_status == "queued"
        assert "private-voice" not in progress[0].source_summary
    finally:
        await database.close()
