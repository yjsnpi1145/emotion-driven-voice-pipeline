from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.director_preprocessing import (
    PreprocessingService,
    _run_fail_fast,
    validate_preprocess_rewrite,
)
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import CreateDirectorProjectRequest
from voice_pipeline.models.director_llm import (
    PreprocessRewriteItem,
    PreprocessRewriteResult,
    PreprocessRewriteUnit,
)
from voice_pipeline.modules.text.structural_cleaner import StructuralTextCleaner
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
        elif self.failure == "pause_to_text" and items:
            index = next(
                index for index, unit in enumerate(units) if unit.context == "pause_marker"
            )
            items[index] = items[index].model_copy(update={"rewritten_text": "突然开口"})
        elif self.failure == "raise":
            raise RuntimeError("fixture rewrite failure")
        return PreprocessRewriteResult(items=tuple(items))


class BlockingRewriteDirector(RewriteDirector):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def rewrite_preprocess_paragraph(
        self,
        *,
        paragraph_id: str,
        units: tuple[PreprocessRewriteUnit, ...],
    ) -> PreprocessRewriteResult:
        self.calls.append((paragraph_id, units))
        self.started.set()
        await self.release.wait()
        return PreprocessRewriteResult(
            items=tuple(
                PreprocessRewriteItem(
                    unit_id=unit.unit_id,
                    rewritten_text=unit.text,
                    input_unit_ids=(unit.unit_id,),
                )
                for unit in units
            )
        )


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
async def test_skip_mode_preserves_source_text_exactly(store: DirectorStore) -> None:
    source = "\n  第一段。\r\n\r\n\r\n第二段。  \n"
    project = await store.create_project(request(mode="skip", source=source))
    director = RewriteDirector()

    result = await PreprocessingService(store, director).run(
        project.project_id,
        expected_revision=project.revision,
    )

    assert result.structural_text == source
    assert result.preprocessed_text == source
    assert director.calls == []
    page = await store.list_preprocess_paragraphs(project.project_id)
    assert len(page.items) == 1
    assert page.items[0].preprocessed_text == source


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


@pytest.mark.asyncio
async def test_manual_rewrite_request_falls_back_when_llm_call_fails(
    store: DirectorStore,
) -> None:
    project = await store.create_project(request(mode="rewrite"))
    service = PreprocessingService(store, RewriteDirector())
    project = await service.run(
        project.project_id,
        expected_revision=project.revision,
    )
    paragraph = (await store.list_preprocess_paragraphs(project.project_id)).items[0]
    service = PreprocessingService(store, RewriteDirector("raise"))

    rewritten = await service.rewrite_paragraph(
        project.project_id,
        paragraph.paragraph_id,
        expected_project_revision=project.revision,
        expected_revision=paragraph.revision,
    )

    assert rewritten.rewrite_state == "fallback"
    assert rewritten.preprocessed_text == rewritten.structural_text
    assert rewritten.validation is not None
    assert rewritten.validation["code"] == "LLM_UNAVAILABLE"


def test_rewrite_rejects_turning_pause_marker_into_spoken_text() -> None:
    paragraph = StructuralTextCleaner().clean("……").paragraphs[0]
    unit = paragraph.units[0]
    result = PreprocessRewriteResult(
        items=(
            PreprocessRewriteItem(
                unit_id=unit.unit_id,
                rewritten_text="突然开口",
                input_unit_ids=(unit.unit_id,),
            ),
        )
    )

    with pytest.raises(PipelineError):
        validate_preprocess_rewrite(paragraph, result)


def test_identity_rewrite_accepts_quote_spanning_paragraphs() -> None:
    document = StructuralTextCleaner().clean("“第一段对白\n\n第二段对白”")

    for paragraph in document.paragraphs:
        result = PreprocessRewriteResult(
            items=tuple(
                PreprocessRewriteItem(
                    unit_id=unit.unit_id,
                    rewritten_text=unit.text,
                    input_unit_ids=(unit.unit_id,),
                )
                for unit in paragraph.units
            )
        )
        assert validate_preprocess_rewrite(paragraph, result) == paragraph.structural_text


@pytest.mark.asyncio
async def test_two_projects_with_identical_source_have_distinct_paragraph_ids(
    store: DirectorStore,
) -> None:
    first = await store.create_project(request(mode="structural", source="相同正文。"))
    second = await store.create_project(request(mode="structural", source="相同正文。"))
    service = PreprocessingService(store, RewriteDirector())

    await service.run(first.project_id, expected_revision=first.revision)
    await service.run(second.project_id, expected_revision=second.revision)
    first_row = (await store.list_preprocess_paragraphs(first.project_id)).items[0]
    second_row = (await store.list_preprocess_paragraphs(second.project_id)).items[0]

    assert first_row.paragraph_id != second_row.paragraph_id


@pytest.mark.asyncio
async def test_manual_rewrite_validates_revision_before_single_flight_llm_call(
    store: DirectorStore,
) -> None:
    project = await store.create_project(request(mode="rewrite"))
    project = await PreprocessingService(store, RewriteDirector()).run(
        project.project_id,
        expected_revision=project.revision,
    )
    paragraph = (await store.list_preprocess_paragraphs(project.project_id)).items[0]
    director = BlockingRewriteDirector()
    service = PreprocessingService(store, director)

    first = asyncio.create_task(
        service.rewrite_paragraph(
            project.project_id,
            paragraph.paragraph_id,
            expected_project_revision=project.revision,
            expected_revision=paragraph.revision,
        )
    )
    await director.started.wait()
    second = asyncio.create_task(
        service.rewrite_paragraph(
            project.project_id,
            paragraph.paragraph_id,
            expected_project_revision=project.revision,
            expected_revision=paragraph.revision,
        )
    )
    director.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert len(director.calls) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    error = next(result for result in results if isinstance(result, PipelineError))
    assert error.code == ErrorCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_retry_reuses_successful_paragraphs_and_only_rewrites_fallbacks(
    store: DirectorStore,
) -> None:
    project = await store.create_project(
        request(mode="rewrite", source="第一段。\n\n第二段。")
    )
    project = await PreprocessingService(store, RewriteDirector()).run(
        project.project_id,
        expected_revision=project.revision,
    )
    before = await store.list_preprocess_paragraphs(project.project_id)
    first_revision = before.items[0].revision
    failed = await PreprocessingService(store, RewriteDirector("raise")).rewrite_paragraph(
        project.project_id,
        before.items[1].paragraph_id,
        expected_project_revision=project.revision,
        expected_revision=before.items[1].revision,
    )
    assert failed.rewrite_state == "fallback"
    project = await store.get_project(project.project_id)
    retry_director = RewriteDirector()

    project = await PreprocessingService(store, retry_director).run(
        project.project_id,
        expected_revision=project.revision,
    )
    after = await store.list_preprocess_paragraphs(project.project_id)

    assert project.status == "preprocess_review"
    assert len(retry_director.calls) == 1
    assert after.items[0].revision == first_revision
    assert [item.rewrite_state for item in after.items] == ["succeeded", "succeeded"]


@pytest.mark.asyncio
async def test_preprocessing_fail_fast_cancels_sibling_tasks() -> None:
    cancelled = asyncio.Event()

    async def fail() -> None:
        await asyncio.sleep(0)
        raise PipelineError(
            ErrorCode.INVALID_INPUT,
            "director",
            "fixture failure",
            retryable=False,
        )

    async def block() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(PipelineError):
        await _run_fail_fast(fail(), block())

    assert cancelled.is_set()
