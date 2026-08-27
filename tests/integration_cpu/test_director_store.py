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
from voice_pipeline.modules.text.structural_cleaner import StructuralTextCleaner
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


async def confirmed_for_analysis(store: DirectorStore, project):
    project = await store.begin_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )
    document = StructuralTextCleaner().clean(
        project.source_text,
        namespace=str(project.project_id),
    )
    await store.stage_preprocess_document(
        project.project_id,
        expected_revision=project.revision,
        document=document,
    )
    project = await store.complete_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )
    return await store.confirm_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )


@pytest.mark.asyncio
async def test_publish_analysis_requires_exact_contiguous_source_coverage(store: DirectorStore):
    project = await store.create_project(project_request())
    project = await confirmed_for_analysis(store, project)
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
async def test_preprocessing_document_is_revisioned_editable_and_confirmable(
    store: DirectorStore,
) -> None:
    source = "第一段。\r\n\r\n第二段。"
    project = await store.create_project(project_request(source))
    assert project.preprocessing_mode == "structural"
    assert project.structural_text is None
    assert project.preprocessed_text is None
    assert project.preprocess_revision == 0

    project = await store.begin_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )
    assert project.status == "preprocessing"
    document = StructuralTextCleaner().clean(source)
    await store.stage_preprocess_document(
        project.project_id,
        expected_revision=project.revision,
        document=document,
    )
    project = await store.complete_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )

    assert project.status == "preprocess_review"
    assert project.structural_text == "第一段。\n\n第二段。"
    assert project.preprocessed_text == project.structural_text
    assert project.preprocess_revision == 1
    page = await store.list_preprocess_paragraphs(
        project.project_id,
        offset=0,
        limit=1,
    )
    assert page.total_count == 2
    assert len(page.items) == 1
    assert page.next_offset == 1
    first = page.items[0]

    updated = await store.patch_preprocess_paragraph(
        project.project_id,
        first.paragraph_id,
        expected_project_revision=project.revision,
        expected_revision=first.revision,
        preprocessed_text="第一段，用户修改。",
    )
    project = await store.get_project(project.project_id)
    assert updated.rewrite_state == "user_edited"
    assert updated.revision == first.revision + 1
    assert project.preprocessed_text == "第一段，用户修改。\n\n第二段。"
    assert project.preprocess_revision == 2

    restored = await store.restore_preprocess_paragraph(
        project.project_id,
        first.paragraph_id,
        expected_project_revision=project.revision,
        expected_revision=updated.revision,
        target="structural",
    )
    project = await store.get_project(project.project_id)
    assert restored.preprocessed_text == "第一段。"
    assert project.preprocessed_text == "第一段。\n\n第二段。"

    with pytest.raises(PipelineError) as exc:
        await store.patch_preprocess_paragraph(
            project.project_id,
            first.paragraph_id,
            expected_project_revision=0,
            expected_revision=restored.revision,
            preprocessed_text="过期写入。",
        )
    assert exc.value.code == ErrorCode.VERSION_CONFLICT

    confirmed = await store.confirm_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )
    assert confirmed.status == "analyzing"
    assert await store.analysis_text(project.project_id) == "第一段。\n\n第二段。"


@pytest.mark.asyncio
async def test_analysis_cannot_bypass_preprocessing_confirmation(
    store: DirectorStore,
) -> None:
    project = await store.create_project(project_request("旁白。"))

    with pytest.raises(PipelineError) as exc:
        await store.begin_analysis(
            project.project_id,
            expected_revision=project.revision,
        )

    assert exc.value.code == ErrorCode.DIRECTOR_STATE_CONFLICT


@pytest.mark.asyncio
async def test_publish_split_merge_and_occ(store: DirectorStore):
    source = "旁白。甲说你好。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
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
    assert [item.working_text for item in utterances] == [item.source_text for item in utterances]
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
async def test_working_text_edit_preserves_source_and_stays_in_role_review(store: DirectorStore):
    source = "甲：原始台词。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
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
            ),
        ),
    )
    utterance = (await store.list_utterances(project.project_id))[0]

    updated = await store.patch_utterance(
        utterance.utterance_id,
        expected_revision=utterance.revision,
        working_text=" 甲：修改后的台词。 ",
    )
    refreshed = await store.get_project(project.project_id)

    assert updated.source_text == source
    assert updated.working_text == " 甲：修改后的台词。 "
    assert updated.revision == utterance.revision + 1
    assert updated.synthesis_text is None
    assert updated.ref_text_cn is None
    assert updated.emotion_vector is None
    assert refreshed.status == "role_review"
    assert refreshed.revision == project.revision + 1

    with pytest.raises(PipelineError) as exc:
        await store.split_utterance(
            updated.utterance_id,
            expected_revision=updated.revision,
            split_at=3,
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_working_text_edit_is_rejected_after_role_review(store: DirectorStore):
    source = "甲：你好。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
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
            ),
        ),
    )
    project = await store.confirm_role_review(
        project.project_id,
        expected_revision=project.revision,
    )
    utterance = (await store.list_utterances(project.project_id))[0]

    with pytest.raises(PipelineError) as exc:
        await store.patch_utterance(
            utterance.utterance_id,
            expected_revision=utterance.revision,
            working_text="越过复核阶段的修改",
        )

    assert exc.value.code == ErrorCode.DIRECTOR_STATE_CONFLICT


@pytest.mark.asyncio
async def test_merge_concatenates_source_and_working_text_independently(store: DirectorStore):
    source = "第一句。第二句。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
    await store.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="旁白", kind="narrator"),),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=4,
                source_text=source[:4],
                kind="narration",
                speak_enabled=True,
            ),
            CreateDirectorUtterance(
                ordinal=1,
                source_start=4,
                source_end=len(source),
                source_text=source[4:],
                kind="narration",
                speak_enabled=True,
            ),
        ),
    )
    left, right = await store.list_utterances(project.project_id)
    left = await store.patch_utterance(
        left.utterance_id,
        expected_revision=left.revision,
        working_text="修改一。",
    )
    right = await store.patch_utterance(
        right.utterance_id,
        expected_revision=right.revision,
        working_text="修改二。",
    )

    merged = await store.merge_utterances(
        left.utterance_id,
        right.utterance_id,
        expected_left_revision=left.revision,
        expected_right_revision=right.revision,
    )

    assert len(merged) == 1
    assert merged[0].source_text == source
    assert merged[0].working_text == "修改一。修改二。"


@pytest.mark.asyncio
async def test_narration_toggle_preserves_source_rows(store: DirectorStore):
    source = "旁白。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
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
async def test_split_role_creates_character_and_reassigns_selected_rows(store: DirectorStore):
    source = "甲：第一句。甲：第二句。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
    project = await store.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="甲", kind="character"),),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=6,
                source_text=source[:6],
                kind="dialogue",
                speak_enabled=True,
                role_name="甲",
            ),
            CreateDirectorUtterance(
                ordinal=1,
                source_start=6,
                source_end=len(source),
                source_text=source[6:],
                kind="dialogue",
                speak_enabled=True,
                role_name="甲",
            ),
        ),
    )
    role = (await store.list_roles(project.project_id))[0]
    rows = await store.list_utterances(project.project_id)
    roles = await store.split_role(
        project.project_id,
        source_role_id=role.role_id,
        utterance_ids=(rows[1].utterance_id,),
        canonical_name="乙",
        expected_project_revision=project.revision,
    )
    assert {item.canonical_name for item in roles} == {"甲", "乙"}
    reassigned = await store.list_utterances(project.project_id)
    names = {item.role_id: item.canonical_name for item in roles}
    assert [names[item.role_id] for item in reassigned] == ["甲", "乙"]


@pytest.mark.asyncio
async def test_review_and_translation_are_explicit_gates(store: DirectorStore):
    source = "甲：你好。"
    project = await store.create_project(project_request(source))
    project = await confirmed_for_analysis(store, project)
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
