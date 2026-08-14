from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.director_analysis import ScriptAnalysisService
from voice_pipeline.models.director import CreateDirectorProjectRequest
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.director_store import DirectorStore


class CountingDirector(FakeDirector):
    def __init__(self) -> None:
        self.analysis_calls = 0

    async def analyze_script_chunk(self, **kwargs):
        self.analysis_calls += 1
        return await super().analyze_script_chunk(**kwargs)


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
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    first_calls = director.analysis_calls
    project = await service.analyze(project.project_id, expected_revision=project.revision)
    assert director.analysis_calls == first_calls
