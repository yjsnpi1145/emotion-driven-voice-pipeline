from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateDirectorProjectRequest,
    CreateDirectorRole,
    CreateDirectorUtterance,
)
from voice_pipeline.models.director_llm import TranslationResultItem
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.director_store import DirectorStore


@pytest.fixture
async def store(tmp_path: Path):
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
    try:
        yield DirectorStore(database)
    finally:
        await database.close()


def project_request(source: str = "旁白。甲说你好。") -> CreateDirectorProjectRequest:
    return CreateDirectorProjectRequest(
        title="测试剧本",
        source_text=source,
        source_language="zh",
        target_language="ja",
    )


@pytest.mark.asyncio
async def test_publish_analysis_requires_exact_contiguous_source_coverage(store: DirectorStore):
    project = await store.create_project(project_request())
    with pytest.raises(PipelineError) as exc:
        await store.publish_analysis(
            project.project_id,
            expected_revision=project.revision,
            roles=(CreateDirectorRole(canonical_name="旁白", kind="narrator"),),
            utterances=(
                CreateDirectorUtterance(
                    ordinal=0,
                    source_start=0,
                    source_end=2,
                    source_text="旁白",
                    kind="narration",
                    speak_enabled=True,
                ),
            ),
        )
    assert exc.value.code == ErrorCode.DIRECTOR_SOURCE_COVERAGE_INVALID


@pytest.mark.asyncio
async def test_publish_split_merge_and_occ(store: DirectorStore):
    source = "旁白。甲说你好。"
    project = await store.create_project(project_request(source))
    project = await store.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(
            CreateDirectorRole(canonical_name="旁白", kind="narrator"),
            CreateDirectorRole(canonical_name="甲", kind="character"),
        ),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=3,
                source_text=source[0:3],
                kind="narration",
                speak_enabled=True,
            ),
            CreateDirectorUtterance(
                ordinal=1,
                source_start=3,
                source_end=len(source),
                source_text=source[3:],
                kind="dialogue",
                speak_enabled=True,
            ),
        ),
    )
    assert project.status == "role_review"
    utterances = await store.list_utterances(project.project_id)
    split = await store.split_utterance(
        utterances[1].utterance_id,
        expected_revision=utterances[1].revision,
        split_at=5,
    )
    assert "".join(row.source_text for row in split) == source
    merged = await store.merge_utterances(
        split[1].utterance_id,
        split[2].utterance_id,
        expected_left_revision=split[1].revision,
        expected_right_revision=split[2].revision,
    )
    assert "".join(row.source_text for row in merged) == source
    with pytest.raises(PipelineError) as exc:
        await store.patch_utterance(
            merged[1].utterance_id,
            expected_revision=99,
            role_id=None,
            role_confirmed=False,
        )
    assert exc.value.code == ErrorCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_narration_toggle_preserves_source_rows(store: DirectorStore):
    source = "旁白。"
    project = await store.create_project(project_request(source))
    project = await store.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="旁白", kind="narrator"),),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=len(source),
                source_text=source,
                kind="narration",
                speak_enabled=True,
            ),
        ),
    )
    updated = await store.set_narration_enabled(
        project.project_id, expected_revision=project.revision, enabled=False
    )
    rows = await store.list_utterances(project.project_id)
    assert updated.narration_enabled is False
    assert rows[0].source_text == source
    assert rows[0].speak_enabled is False


@pytest.mark.asyncio
async def test_review_and_translation_are_explicit_gates(store: DirectorStore):
    source = "甲：你好。"
    project = await store.create_project(project_request(source))
    project = await store.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="甲", kind="character"),),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=len(source),
                source_text=source,
                kind="dialogue",
                speak_enabled=True,
                role_name="甲",
                role_confidence=0.4,
                role_confirmed=False,
            ),
        ),
    )
    with pytest.raises(PipelineError) as exc:
        await store.confirm_role_review(project.project_id, expected_revision=project.revision)
    assert exc.value.code == ErrorCode.DIRECTOR_REVIEW_REQUIRED

    utterance = (await store.list_utterances(project.project_id))[0]
    await store.patch_utterance(
        utterance.utterance_id,
        expected_revision=utterance.revision,
        role_id=utterance.role_id,
        role_confirmed=True,
    )
    project = await store.get_project(project.project_id)
    project = await store.confirm_role_review(
        project.project_id, expected_revision=project.revision
    )
    assert project.status == "translating"

    utterance = (await store.list_utterances(project.project_id))[0]
    project = await store.publish_translation(
        project.project_id,
        expected_revision=project.revision,
        items=(
            TranslationResultItem(
                utterance_id=utterance.utterance_id,
                revision=utterance.revision,
                synthesis_text="こんにちは。",
                ref_text_cn="你好。",
                emotion_vector=[0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ),
        ),
    )
    assert project.status == "translation_review"
    project = await store.confirm_translation(
        project.project_id, expected_revision=project.revision
    )
    assert project.status == "voice_mapping"
