from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.director_analysis import ScriptAnalysisService
from voice_pipeline.core.director_preprocessing import PreprocessingService
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateDirectorProjectRequest,
    CreateDirectorRole,
    CreateDirectorUtterance,
)
from voice_pipeline.models.director_llm import UnitAnalysis, UnitAnalysisResult
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.script_chunking import (
    build_analysis_units,
    materialize_unit_analysis,
    split_script,
)
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.director_store import DirectorStore


class CountingDirector(FakeDirector):
    def __init__(self) -> None:
        self.analysis_calls = 0

    async def analyze_script_chunk(self, **kwargs):
        self.analysis_calls += 1
        return await super().analyze_script_chunk(**kwargs)


class ClassificationOnlyDirector(FakeDirector):
    async def analyze_script_chunk(self, *, chunk, **kwargs):
        del kwargs
        units = build_analysis_units(chunk)
        annotations = UnitAnalysisResult(
            units=tuple(
                UnitAnalysis(
                    unit_id=unit.unit_id,
                    kind="narration",
                    temporary_role_name=None,
                    role_aliases=(),
                    role_confidence=0.9,
                    speak_enabled=True,
                )
                for unit in units
            )
        )
        return materialize_unit_analysis(chunk, units, annotations)


class CapturingDirector(FakeDirector):
    def __init__(self) -> None:
        self.translation_inputs = []

    async def translate_utterances(self, *, target_language, utterances, **kwargs):
        self.translation_inputs.extend(utterances)
        return await super().translate_utterances(
            target_language=target_language,
            utterances=utterances,
            **kwargs,
        )


class FailingBatchDirector(FakeDirector):
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0
        self.release = asyncio.Event()

    async def translate_utterances(self, *, target_language, utterances, **kwargs):
        del target_language, utterances, kwargs
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.02)
            raise PipelineError(
                ErrorCode.LLM_UNAVAILABLE,
                "llm",
                "fixture batch failure",
                retryable=True,
            )
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.fixture
async def resources(tmp_path: Path):
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


async def confirmed_for_analysis(
    store: DirectorStore,
    project,
):
    project = await PreprocessingService(store, FakeDirector()).run(
        project.project_id,
        expected_revision=project.revision,
    )
    return await store.confirm_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )


@pytest.mark.asyncio
async def test_analysis_and_translation_are_separate_and_use_no_gpu(resources: DirectorStore):
    source = "旁白。\n甲：你好。\n乙：再见。"
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="场景",
            source_text=source,
            source_language="zh",
            target_language="ja",
        )
    )
    director = CountingDirector()
    service = ScriptAnalysisService(resources, director, max_chunk_chars=10)
    project = await confirmed_for_analysis(resources, project)
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    assert project.status == "role_review"
    assert director.analysis_calls >= 2
    stored_utterances = await resources.list_utterances(project.project_id)
    assert "".join(item.source_text for item in stored_utterances) == source

    utterances = await resources.list_utterances(project.project_id)
    for item in utterances:
        if item.speak_enabled and not item.role_confirmed:
            await resources.patch_utterance(
                item.utterance_id,
                expected_revision=item.revision,
                role_id=item.role_id,
                role_confirmed=True,
            )
    project = await resources.get_project(project.project_id)
    project = await resources.confirm_role_review(
        project.project_id, expected_revision=project.revision
    )
    project = await service.translate(project.project_id, expected_revision=project.revision)
    assert project.status == "translation_review"
    assert all(
        item.synthesis_text and item.ref_text_cn
        for item in await resources.list_utterances(project.project_id)
        if item.speak_enabled
    )


@pytest.mark.asyncio
async def test_successful_analysis_chunks_are_reused(resources: DirectorStore):
    source = "甲：一。乙：二。丙：三。"
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="恢复",
            source_text=source,
            source_language="zh",
            target_language="en",
        )
    )
    director = CountingDirector()
    service = ScriptAnalysisService(resources, director, max_chunk_chars=8)
    project = await confirmed_for_analysis(resources, project)
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    first_calls = director.analysis_calls
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    assert director.analysis_calls == first_calls


@pytest.mark.asyncio
async def test_analysis_cache_requires_the_current_contract_metadata(
    resources: DirectorStore,
) -> None:
    source = "甲：一。乙：二。"
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="缓存版本",
            source_text=source,
            source_language="zh",
            target_language="en",
        )
    )
    chunk = split_script(source, max_chars=100)[0]
    result = await FakeDirector().analyze_script_chunk(chunk=chunk)
    await resources.save_analysis_chunk(
        project.project_id,
        ordinal=0,
        chunk=chunk,
        result=result,
        llm_fingerprint="runtime-director-unit-ids-v2",
        prompt_version="director-analysis-units-v2",
        schema_version=2,
    )

    assert (
        await resources.load_analysis_chunk(
            project.project_id,
            chunk,
            llm_fingerprint="runtime-director-quote-units-v3",
            prompt_version="director-analysis-quote-units-v3",
            schema_version=3,
        )
        is None
    )

    director = CountingDirector()
    service = ScriptAnalysisService(resources, director, max_chunk_chars=100)
    project = await confirmed_for_analysis(resources, project)
    await service.analyze(project.project_id, expected_revision=project.revision)
    assert director.analysis_calls == 1

    await resources.save_analysis_chunk(
        project.project_id,
        ordinal=0,
        chunk=chunk,
        result=result,
        llm_fingerprint="runtime-director-quote-units-v3",
        prompt_version="director-analysis-quote-units-v3",
        schema_version=3,
    )
    cached = await resources.load_analysis_chunk(
        project.project_id,
        chunk,
        llm_fingerprint="runtime-director-quote-units-v3",
        prompt_version="director-analysis-quote-units-v3",
        schema_version=3,
    )
    assert cached == result


@pytest.mark.asyncio
async def test_translation_uses_edited_working_text_instead_of_source(
    resources: DirectorStore,
) -> None:
    source = "甲：原始台词。"
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="可编辑台词",
            source_text=source,
            source_language="zh",
            target_language="ja",
        )
    )
    director = CapturingDirector()
    service = ScriptAnalysisService(resources, director)
    project = await confirmed_for_analysis(resources, project)
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    utterance = (await resources.list_utterances(project.project_id))[0]
    updated = await resources.patch_utterance(
        utterance.utterance_id,
        expected_revision=utterance.revision,
        working_text="甲：修改后才进入翻译的台词。",
    )
    project = await resources.get_project(project.project_id)
    project = await resources.confirm_role_review(
        project.project_id,
        expected_revision=project.revision,
    )

    await service.translate(project.project_id, expected_revision=project.revision)
    stored = (await resources.list_utterances(project.project_id))[0]

    assert updated.source_text == source
    assert [item.source_text for item in director.translation_inputs] == [
        "甲：修改后才进入翻译的台词。"
    ]
    assert stored.source_text == source
    assert stored.working_text == "甲：修改后才进入翻译的台词。"
    assert stored.synthesis_text == "甲：修改后才进入翻译的台词。"


@pytest.mark.asyncio
async def test_classification_only_analysis_preserves_long_unicode_source(
    resources: DirectorStore,
) -> None:
    source = "旁白：" + "这是一段不应交给模型计算下标的中文原文。" * 18
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="稳定切片",
            source_text=source,
            source_language="zh",
            target_language="ja",
        )
    )
    service = ScriptAnalysisService(
        resources,
        ClassificationOnlyDirector(),
        max_chunk_chars=2400,
    )

    project = await confirmed_for_analysis(resources, project)
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    stored = await resources.list_utterances(project.project_id)

    assert project.status == "role_review"
    assert "".join(item.source_text for item in stored) == source
    assert stored[0].source_start == 0
    assert stored[-1].source_end == len(source)
    assert all(
        left.source_end == right.source_start
        for left, right in zip(stored, stored[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_analysis_consumes_confirmed_preprocessed_text(
    resources: DirectorStore,
) -> None:
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="确认稿",
            source_text="原始第一段。\n\n原始第二段。",
            source_language="zh",
            target_language="ja",
            preprocessing_mode="structural",
        )
    )
    project = await PreprocessingService(resources, FakeDirector()).run(
        project.project_id,
        expected_revision=project.revision,
    )
    page = await resources.list_preprocess_paragraphs(project.project_id)
    first = page.items[0]
    await resources.patch_preprocess_paragraph(
        project.project_id,
        first.paragraph_id,
        expected_project_revision=project.revision,
        expected_revision=first.revision,
        preprocessed_text="用户确认后的第一段。",
    )
    project = await resources.get_project(project.project_id)
    project = await resources.confirm_preprocessing(
        project.project_id,
        expected_revision=project.revision,
    )

    result = await ScriptAnalysisService(
        resources,
        ClassificationOnlyDirector(),
    ).analyze(project.project_id, expected_revision=project.revision)
    utterances = await resources.list_utterances(project.project_id)

    assert result.status == "role_review"
    assert "".join(row.source_text for row in utterances) == (
        "用户确认后的第一段。\n\n原始第二段。"
    )
    assert (await resources.get_project(project.project_id)).source_text == (
        "原始第一段。\n\n原始第二段。"
    )


@pytest.mark.asyncio
async def test_translation_rejects_spoken_punctuation_before_llm_call(
    resources: DirectorStore,
) -> None:
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="标点预检",
            source_text="甲。……",
            source_language="zh",
            target_language="ja",
        )
    )
    project = await confirmed_for_analysis(resources, project)
    project = await resources.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="旁白", kind="narrator"),),
        utterances=(
            CreateDirectorUtterance(
                ordinal=0,
                source_start=0,
                source_end=2,
                source_text="甲。",
                kind="narration",
                speak_enabled=False,
            ),
            CreateDirectorUtterance(
                ordinal=1,
                source_start=2,
                source_end=4,
                source_text="……",
                kind="narration",
                speak_enabled=True,
            ),
        ),
    )
    project = await resources.confirm_role_review(
        project.project_id,
        expected_revision=project.revision,
    )
    director = CapturingDirector()

    with pytest.raises(PipelineError) as exc:
        await ScriptAnalysisService(resources, director).translate(
            project.project_id,
            expected_revision=project.revision,
        )

    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert director.translation_inputs == []


@pytest.mark.asyncio
async def test_translation_cancels_sibling_batches_after_first_failure(
    resources: DirectorStore,
) -> None:
    source = "甲。乙。丙。"
    project = await resources.create_project(
        CreateDirectorProjectRequest(
            title="取消并行批",
            source_text=source,
            source_language="zh",
            target_language="ja",
        )
    )
    project = await confirmed_for_analysis(resources, project)
    project = await resources.publish_analysis(
        project.project_id,
        expected_revision=project.revision,
        roles=(CreateDirectorRole(canonical_name="旁白", kind="narrator"),),
        utterances=tuple(
            CreateDirectorUtterance(
                ordinal=index,
                source_start=index * 2,
                source_end=index * 2 + 2,
                source_text=source[index * 2 : index * 2 + 2],
                kind="narration",
                speak_enabled=True,
            )
            for index in range(3)
        ),
    )
    project = await resources.confirm_role_review(
        project.project_id,
        expected_revision=project.revision,
    )
    director = FailingBatchDirector()

    with pytest.raises(PipelineError):
        await ScriptAnalysisService(
            resources,
            director,
            translation_batch_size=1,
        ).translate(project.project_id, expected_revision=project.revision)
    await asyncio.sleep(0)
    director.release.set()

    assert director.calls == 3
    assert director.cancelled == 2
