from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.director_preprocessing import PreprocessingService
from voice_pipeline.models.director import CreateDirectorProjectRequest
from voice_pipeline.models.director_llm import (
    PreprocessRewriteItem,
    PreprocessRewriteResult,
    PreprocessRewriteUnit,
)
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.director_store import DirectorStore


class RewriteDirector:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, tuple[PreprocessRewriteUnit, ...]]] = []

    async def rewrite_preprocess_paragraph(
        self,
        *,
        paragraph_id: str,
        units: tuple[PreprocessRewriteUnit, ...],
    ) -> PreprocessRewriteResult:
        self.calls.append((paragraph_id, units))
        items = [
            PreprocessRewriteItem(
                unit_id=unit.unit_id,
                rewritten_text=unit.text,
                input_unit_ids=(unit.unit_id,),
            )
            for unit in units
        ]
        if self.failure == "missing":
            items = items[:-1]
        elif self.failure == "duplicate" and items:
            items.append(items[-1])
        elif self.failure == "reordered":
            items.reverse()
        elif self.failure == "blank" and items:
            items[0] = items[0].model_copy(update={"rewritten_text": "  "})
        elif self.failure == "overlong" and items:
            items[0] = items[0].model_copy(
                update={"rewritten_text": items[0].rewritten_text * 8}
            )
        elif self.failure == "language" and items:
            items[0] = items[0].model_copy(
                update={"rewritten_text": "This is a fully translated replacement."}
            )
        elif self.failure == "protected" and items:
            items[0] = items[0].model_copy(
                update={"rewritten_text": "“陛下，这是一笔债务。”"}
            )
        elif self.failure == "quotes" and items:
            items[0] = items[0].model_copy(
                update={"rewritten_text": items[0].rewritten_text.strip("“”")}
            )
        return PreprocessRewriteResult(items=tuple(items))


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


def request(
    *,
    mode: str,
    source: str = "“Your Majesty，欠款168万。”她低头说，“会还的。”",
) -> CreateDirectorProjectRequest:
    return CreateDirectorProjectRequest(
        title="预处理",
        source_text=source,
        source_language="auto",
        target_language="ja",
        preprocessing_mode=mode,
    )


@pytest.mark.asyncio
async def test_structural_mode_does_not_call_llm(store: DirectorStore) -> None:
    project = await store.create_project(request(mode="structural", source="第一段。\n\n第二段。"))
    director = RewriteDirector()

    result = await PreprocessingService(store, director).run(
        project.project_id,
        expected_revision=project.revision,
    )

    assert result.status == "preprocess_review"
    assert result.preprocessed_text == result.structural_text
    assert director.calls == []
    page = await store.list_preprocess_paragraphs(project.project_id)
    assert [row.rewrite_state for row in page.items] == ["local", "local"]


@pytest.mark.asyncio
async def test_rewrite_mode_preserves_stable_unit_contract(store: DirectorStore) -> None:
    project = await store.create_project(request(mode="rewrite"))
    director = RewriteDirector()

    result = await PreprocessingService(store, director).run(
        project.project_id,
        expected_revision=project.revision,
    )

    assert result.status == "preprocess_review"
    assert len(director.calls) == 1
    paragraph_id, units = director.calls[0]
    assert paragraph_id
    assert [unit.context for unit in units] == [
        "quoted_dialogue",
        "quote_bridge_narration",
        "quoted_dialogue",
    ]
    assert all(unit.unit_id for unit in units)
    page = await store.list_preprocess_paragraphs(project.project_id)
    assert page.items[0].rewrite_state == "succeeded"
    assert page.items[0].validation is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "reordered",
        "blank",
        "overlong",
        "language",
        "protected",
        "quotes",
    ],
)
async def test_invalid_rewrite_falls_back_to_local_paragraph(
    store: DirectorStore,
    failure: str,
) -> None:
    project = await store.create_project(request(mode="rewrite"))
    director = RewriteDirector(failure)

    result = await PreprocessingService(store, director).run(
        project.project_id,
        expected_revision=project.revision,
    )

    assert result.status == "preprocess_review"
    page = await store.list_preprocess_paragraphs(project.project_id)
    paragraph = page.items[0]
    assert paragraph.rewrite_state == "fallback"
    assert paragraph.preprocessed_text == paragraph.structural_text
    assert paragraph.validation is not None
    assert paragraph.validation["code"] == "LLM_INVALID_RESPONSE"
