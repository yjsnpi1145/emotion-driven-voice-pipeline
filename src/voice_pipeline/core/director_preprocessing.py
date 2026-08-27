from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    DirectorPreprocessParagraphRecord,
    DirectorProjectRecord,
)
from voice_pipeline.models.director_llm import (
    PreprocessRewriteResult,
    PreprocessRewriteUnit,
)
from voice_pipeline.modules.text.structural_cleaner import (
    StructuralParagraph,
    StructuralTextCleaner,
)
from voice_pipeline.storage.director_store import DirectorStore

_PROTECTED_TOKEN = re.compile(
    r"\d+(?:[.,]\d+)?|[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z][A-Za-z0-9_-]*)*"
)
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", '"': '"'}
_ResultT = TypeVar("_ResultT")


class PreprocessDirector(Protocol):
    async def rewrite_preprocess_paragraph(
        self,
        *,
        paragraph_id: str,
        units: tuple[PreprocessRewriteUnit, ...],
    ) -> PreprocessRewriteResult: ...


class PreprocessingService:
    """Create a deterministic review draft with optional constrained LLM rewrites."""

    def __init__(
        self,
        store: DirectorStore,
        director: PreprocessDirector,
        *,
        cleaner: StructuralTextCleaner | None = None,
    ) -> None:
        self._store = store
        self._director = director
        self._cleaner = cleaner or StructuralTextCleaner()
        self._rewrite_locks: dict[tuple[UUID, str], asyncio.Lock] = {}

    async def run(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
    ) -> DirectorProjectRecord:
        project = await self._store.begin_preprocessing(
            project_id,
            expected_revision=expected_revision,
        )
        document = (
            self._cleaner.preserve(project.source_text, namespace=str(project_id))
            if project.preprocessing_mode == "skip"
            else self._cleaner.clean(project.source_text, namespace=str(project_id))
        )
        await self._store.stage_preprocess_document(
            project_id,
            expected_revision=project.revision,
            document=document,
        )
        if project.preprocessing_mode == "rewrite":
            structural_by_id = {
                paragraph.paragraph_id: paragraph
                for paragraph in document.paragraphs
            }
            pending = await self._store.pending_preprocess_paragraphs(project_id)
            await _run_fail_fast(
                *(
                    self._rewrite_staged_paragraph(
                        project,
                        structural_by_id[record.paragraph_id],
                        record,
                    )
                    for record in pending
                )
            )
        return await self._store.complete_preprocessing(
            project_id,
            expected_revision=project.revision,
        )

    async def rewrite_paragraph(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
    ) -> DirectorPreprocessParagraphRecord:
        key = (project_id, paragraph_id)
        lock = self._rewrite_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._rewrite_paragraph_locked(
                project_id,
                paragraph_id,
                expected_project_revision=expected_project_revision,
                expected_revision=expected_revision,
            )

    async def _rewrite_paragraph_locked(
        self,
        project_id: UUID,
        paragraph_id: str,
        *,
        expected_project_revision: int,
        expected_revision: int,
    ) -> DirectorPreprocessParagraphRecord:
        project = await self._store.get_project(project_id)
        if project.revision != expected_project_revision:
            raise _version_conflict()
        if project.status != "preprocess_review":
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director",
                f"cannot rewrite preprocessing paragraph while project is {project.status}",
                retryable=False,
            )
        if project.preprocessing_mode != "rewrite":
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director",
                "paragraph rewrite is available only in rewrite preprocessing mode",
                retryable=False,
            )
        paragraph = await self._store.get_preprocess_paragraph(project_id, paragraph_id)
        if paragraph.revision != expected_revision:
            raise _version_conflict()
        document = self._cleaner.clean(
            paragraph.structural_text,
            namespace=f"{project_id}:{paragraph_id}:review",
        )
        if len(document.paragraphs) != 1:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "director",
                "stored preprocessing paragraph has an invalid structure",
                retryable=False,
            )
        structural = document.paragraphs[0]
        units = tuple(
            PreprocessRewriteUnit(
                unit_id=unit.unit_id,
                text=unit.text,
                context=unit.context,
            )
            for unit in structural.units
        )
        try:
            result = await self._director.rewrite_preprocess_paragraph(
                paragraph_id=paragraph_id,
                units=units,
            )
            rewritten = validate_preprocess_rewrite(structural, result)
        except asyncio.CancelledError:
            raise
        except PipelineError as exc:
            return await self._store.apply_review_preprocess_result(
                project_id,
                paragraph_id,
                expected_project_revision=expected_project_revision,
                expected_revision=expected_revision,
                preprocessed_text=paragraph.structural_text,
                rewrite_state="fallback",
                validation=exc.as_dict(),
            )
        except ValidationError as exc:
            error = _schema_error(paragraph_id, exc)
            return await self._store.apply_review_preprocess_result(
                project_id,
                paragraph_id,
                expected_project_revision=expected_project_revision,
                expected_revision=expected_revision,
                preprocessed_text=paragraph.structural_text,
                rewrite_state="fallback",
                validation=error.as_dict(),
            )
        except Exception as exc:
            error = _request_error(paragraph_id, exc)
            return await self._store.apply_review_preprocess_result(
                project_id,
                paragraph_id,
                expected_project_revision=expected_project_revision,
                expected_revision=expected_revision,
                preprocessed_text=paragraph.structural_text,
                rewrite_state="fallback",
                validation=error.as_dict(),
            )
        return await self._store.apply_review_preprocess_result(
            project_id,
            paragraph_id,
            expected_project_revision=expected_project_revision,
            expected_revision=expected_revision,
            preprocessed_text=rewritten,
            rewrite_state="succeeded",
        )

    async def _rewrite_staged_paragraph(
        self,
        project: DirectorProjectRecord,
        paragraph: StructuralParagraph,
        record: DirectorPreprocessParagraphRecord,
    ) -> None:
        units = tuple(
            PreprocessRewriteUnit(
                unit_id=unit.unit_id,
                text=unit.text,
                context=unit.context,
            )
            for unit in paragraph.units
        )
        if not any(unit.speakable for unit in paragraph.units):
            await self._store.save_preprocess_result(
                project.project_id,
                paragraph.paragraph_id,
                expected_project_revision=project.revision,
                expected_revision=record.revision,
                preprocessed_text=paragraph.structural_text,
                rewrite_state="local",
            )
            return
        try:
            result = await self._director.rewrite_preprocess_paragraph(
                paragraph_id=paragraph.paragraph_id,
                units=units,
            )
            rewritten = validate_preprocess_rewrite(paragraph, result)
        except asyncio.CancelledError:
            raise
        except PipelineError as exc:
            await self._fallback(project, paragraph, record, exc)
            return
        except ValidationError as exc:
            error = _schema_error(paragraph.paragraph_id, exc)
            await self._fallback(project, paragraph, record, error)
            return
        except Exception as exc:
            error = _request_error(paragraph.paragraph_id, exc)
            await self._fallback(project, paragraph, record, error)
            return
        await self._store.save_preprocess_result(
            project.project_id,
            paragraph.paragraph_id,
            expected_project_revision=project.revision,
            expected_revision=record.revision,
            preprocessed_text=rewritten,
            rewrite_state="succeeded",
        )

    async def _fallback(
        self,
        project: DirectorProjectRecord,
        paragraph: StructuralParagraph,
        record: DirectorPreprocessParagraphRecord,
        error: PipelineError,
    ) -> None:
        await self._store.save_preprocess_result(
            project.project_id,
            paragraph.paragraph_id,
            expected_project_revision=project.revision,
            expected_revision=record.revision,
            preprocessed_text=paragraph.structural_text,
            rewrite_state="fallback",
            validation=error.as_dict(),
        )


async def _run_fail_fast(*awaitables: Awaitable[_ResultT]) -> None:
    tasks: tuple[asyncio.Future[_ResultT], ...] = tuple(
        asyncio.ensure_future(awaitable) for awaitable in awaitables
    )
    if not tasks:
        return
    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failure: BaseException | None = None
    for task in done:
        if task.cancelled():
            failure = asyncio.CancelledError()
            break
        error = task.exception()
        if error is not None:
            failure = error
            break
    if failure is not None:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise failure
    await asyncio.gather(*pending)


def _version_conflict() -> PipelineError:
    return PipelineError(
        ErrorCode.VERSION_CONFLICT,
        "director",
        "director data changed; refresh before submitting",
        retryable=False,
    )


def _schema_error(paragraph_id: str, error: ValidationError) -> PipelineError:
    return PipelineError(
        ErrorCode.LLM_INVALID_RESPONSE,
        "llm",
        "LLM preprocessing JSON does not match the required schema",
        retryable=False,
        details={
            "paragraph_id": paragraph_id,
            "schema_errors": [
                {
                    "path": ".".join(str(part) for part in item["loc"]),
                    "type": item["type"],
                }
                for item in error.errors()[:20]
            ],
        },
    )


def _request_error(paragraph_id: str, error: Exception) -> PipelineError:
    return PipelineError(
        ErrorCode.LLM_UNAVAILABLE,
        "llm",
        "LLM preprocessing request failed",
        retryable=True,
        details={
            "paragraph_id": paragraph_id,
            "error_type": type(error).__name__,
        },
    )


def validate_preprocess_rewrite(
    paragraph: StructuralParagraph,
    result: PreprocessRewriteResult,
) -> str:
    expected_ids = tuple(unit.unit_id for unit in paragraph.units)
    actual_ids = tuple(item.unit_id for item in result.items)
    if actual_ids != expected_ids:
        raise _invalid(
            paragraph,
            "preprocessing unit IDs must match exactly and in order",
        )
    rewritten: list[str] = []
    for unit, item in zip(paragraph.units, result.items, strict=True):
        if tuple(item.input_unit_ids) != (unit.unit_id,):
            raise _invalid(
                paragraph,
                "preprocessing input unit coverage must be one-to-one",
                unit_id=unit.unit_id,
            )
        output = item.rewritten_text
        if not output.strip():
            raise _invalid(
                paragraph,
                "preprocessing output must not be blank",
                unit_id=unit.unit_id,
            )
        if not unit.speakable and output != unit.text:
            raise _invalid(
                paragraph,
                "preprocessing must preserve non-spoken formatting and pause markers",
                unit_id=unit.unit_id,
            )
        ratio = len(output) / max(1, len(unit.text))
        if not 0.45 <= ratio <= 2.5:
            raise _invalid(
                paragraph,
                "preprocessing output length changed beyond the allowed range",
                unit_id=unit.unit_id,
            )
        for token in _PROTECTED_TOKEN.findall(unit.text):
            if token not in output:
                raise _invalid(
                    paragraph,
                    f"preprocessing output dropped protected token: {token}",
                    unit_id=unit.unit_id,
                )
        _validate_script_profile(paragraph, unit.unit_id, unit.text, output)
        if unit.context == "quoted_dialogue" and (
            _quote_boundary_signature(unit.text)
            != _quote_boundary_signature(output)
        ):
            raise _invalid(
                paragraph,
                "preprocessing output changed dialogue quote wrappers",
                unit_id=unit.unit_id,
            )
        rewritten.append(output)
    return "".join(rewritten)


def _quote_boundary_signature(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    opener = value[0] if value[0] in _QUOTE_PAIRS else None
    closers = set(_QUOTE_PAIRS.values())
    closer = value[-1] if value[-1] in closers else None
    return opener, closer


def _validate_script_profile(
    paragraph: StructuralParagraph,
    unit_id: str,
    source: str,
    output: str,
) -> None:
    source_scripts = _script_presence(source)
    output_scripts = _script_presence(output)
    for script in ("han", "kana", "hangul"):
        if source_scripts[script] and not output_scripts[script]:
            raise _invalid(
                paragraph,
                "preprocessing output changed the source language profile",
                unit_id=unit_id,
            )
    if source_scripts["latin"] and not output_scripts["latin"]:
        raise _invalid(
            paragraph,
            "preprocessing output dropped Latin-language content",
            unit_id=unit_id,
        )


def _script_presence(value: str) -> dict[str, bool]:
    result = {"han": False, "kana": False, "hangul": False, "latin": False}
    for character in value:
        code = ord(character)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
        ):
            result["han"] = True
        elif 0x3040 <= code <= 0x30FF:
            result["kana"] = True
        elif 0xAC00 <= code <= 0xD7AF:
            result["hangul"] = True
        elif "LATIN" in unicodedata.name(character, ""):
            result["latin"] = True
    return result


def _invalid(
    paragraph: StructuralParagraph,
    message: str,
    *,
    unit_id: str | None = None,
) -> PipelineError:
    details: dict[str, object] = {"paragraph_id": paragraph.paragraph_id}
    if unit_id is not None:
        details["unit_id"] = unit_id
    return PipelineError(
        ErrorCode.LLM_INVALID_RESPONSE,
        "llm",
        message,
        retryable=False,
        details=details,
    )
