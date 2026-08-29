from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Protocol, TypeVar
from uuid import UUID

from voice_pipeline.core.contextual_emotion import (
    build_emotion_inputs,
    normalize_directed_vector,
)
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateDirectorRole,
    CreateDirectorUtterance,
    DirectorProjectRecord,
)
from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    CastReconciliationResult,
    ChunkAnalysisResult,
    EmotionDirectionInput,
    EmotionDirectionResult,
    EmotionDirectionResultItem,
    ScriptChunk,
    ScriptTranslationResult,
    TranslationInput,
    TranslationResultItem,
)
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.script_chunking import split_script, validate_chunk_analysis
from voice_pipeline.modules.text.speakability import is_speakable_text
from voice_pipeline.storage.director_store import DirectorStore

_ANALYSIS_LLM_FINGERPRINT = "runtime-director-quote-units-v3"
_ANALYSIS_PROMPT_VERSION = "director-analysis-quote-units-v3"
_ANALYSIS_SCHEMA_VERSION = 3


class StagedDirector(Protocol):
    async def analyze_script_chunk(self, *, chunk: ScriptChunk) -> ChunkAnalysisResult: ...

    async def reconcile_cast(
        self, *, utterances: tuple[AnalyzedUtterance, ...]
    ) -> CastReconciliationResult: ...

    async def translate_utterances(
        self,
        *,
        target_language: LanguageCode,
        utterances: tuple[TranslationInput, ...],
    ) -> ScriptTranslationResult: ...

    async def direct_emotions(
        self,
        *,
        performance_direction: str | None,
        utterances: tuple[EmotionDirectionInput, ...],
    ) -> EmotionDirectionResult: ...


class ScriptAnalysisService:
    """Resumable LLM-only analysis and translation orchestration."""

    def __init__(
        self,
        store: DirectorStore,
        director: StagedDirector,
        *,
        max_chunk_chars: int = 2400,
        translation_batch_size: int = 40,
    ) -> None:
        self._store = store
        self._director = director
        self._max_chunk_chars = max_chunk_chars
        self._translation_batch_size = translation_batch_size

    async def analyze(self, project_id: UUID, *, expected_revision: int) -> DirectorProjectRecord:
        project = await self._store.begin_analysis(project_id, expected_revision=expected_revision)
        analysis_source = await self._store.analysis_text(project_id)
        chunks = split_script(analysis_source, self._max_chunk_chars)

        async def resolve(ordinal: int, chunk: ScriptChunk) -> ChunkAnalysisResult:
            cached = await self._store.load_analysis_chunk(
                project_id,
                chunk,
                llm_fingerprint=_ANALYSIS_LLM_FINGERPRINT,
                prompt_version=_ANALYSIS_PROMPT_VERSION,
                schema_version=_ANALYSIS_SCHEMA_VERSION,
            )
            if cached is not None:
                validate_chunk_analysis(chunk, cached)
                return cached
            result = await self._director.analyze_script_chunk(chunk=chunk)
            validate_chunk_analysis(chunk, result)
            await self._store.save_analysis_chunk(
                project_id,
                ordinal=ordinal,
                chunk=chunk,
                result=result,
                llm_fingerprint=_ANALYSIS_LLM_FINGERPRINT,
                prompt_version=_ANALYSIS_PROMPT_VERSION,
                schema_version=_ANALYSIS_SCHEMA_VERSION,
            )
            return result

        results = await _gather_fail_fast(
            *(resolve(ordinal, chunk) for ordinal, chunk in enumerate(chunks))
        )
        analyzed = tuple(item for result in results for item in result.utterances)
        cast = await self._director.reconcile_cast(utterances=analyzed)
        roles, utterances = _materialize_cast(analyzed, cast)
        return await self._store.publish_analysis(
            project_id,
            expected_revision=project.revision,
            roles=roles,
            utterances=utterances,
        )

    async def translate(self, project_id: UUID, *, expected_revision: int) -> DirectorProjectRecord:
        project = await self._store.get_project(project_id)
        if project.revision != expected_revision:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "director",
                "director data changed; refresh before translating",
                retryable=False,
            )
        if project.status != "translating":
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director",
                f"cannot translate while project is {project.status}",
                retryable=False,
            )
        timeline = await self._store.list_utterances(project_id)
        spoken = []
        for item in timeline:
            if not item.speak_enabled:
                continue
            if not is_speakable_text(item.working_text):
                raise PipelineError(
                    ErrorCode.INVALID_INPUT,
                    "director",
                    "spoken utterance contains no pronounceable text",
                    retryable=False,
                    details={"utterance_id": str(item.utterance_id)},
                )
            spoken.append(
                TranslationInput(
                    utterance_id=item.utterance_id,
                    revision=item.revision,
                    source_text=item.working_text,
                )
            )
        batches = [
            tuple(spoken[index : index + self._translation_batch_size])
            for index in range(0, len(spoken), self._translation_batch_size)
        ]
        results = await _gather_fail_fast(
            *(
                self._director.translate_utterances(
                    target_language=project.target_language,
                    utterances=batch,
                )
                for batch in batches
            )
        )
        items = tuple(item for result in results for item in result.items)
        roles = await self._store.list_roles(project_id)
        emotion_inputs = build_emotion_inputs(
            utterances=timeline,
            role_names={item.role_id: item.canonical_name for item in roles},
            reviewed_source=await self._store.analysis_text(project_id),
        )
        emotion_batches = [
            emotion_inputs[index : index + self._translation_batch_size]
            for index in range(0, len(emotion_inputs), self._translation_batch_size)
        ]
        directed_results = await _gather_fail_fast(
            *(
                self._director.direct_emotions(
                    performance_direction=project.performance_direction,
                    utterances=batch,
                )
                for batch in emotion_batches
            )
        )
        directed_items = tuple(item for result in directed_results for item in result.items)
        items = _apply_directed_emotions(
            translations=items,
            directions=directed_items,
            expected=tuple(spoken),
        )
        return await self._store.publish_translation(
            project_id,
            expected_revision=project.revision,
            items=items,
        )

    async def direct_current_performance(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        performance_direction: str | None,
    ) -> tuple[EmotionDirectionResultItem, ...]:
        """Re-evaluate only vector, speed and pause for already translated rows."""
        project = await self._store.get_project(project_id)
        if project.revision != expected_revision:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "director",
                "director data changed; refresh before reapplying performance direction",
                retryable=False,
            )
        allowed = {
            "translation_review",
            "voice_mapping",
            "ready",
            "generation_incomplete",
            "succeeded",
        }
        if project.status not in allowed:
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director",
                f"cannot reapply performance direction while project is {project.status}",
                retryable=False,
            )
        timeline = await self._store.list_utterances(project_id)
        roles = await self._store.list_roles(project_id)
        inputs = build_emotion_inputs(
            utterances=timeline,
            role_names={item.role_id: item.canonical_name for item in roles},
            reviewed_source=await self._store.analysis_text(project_id),
        )
        batches = [
            inputs[index : index + self._translation_batch_size]
            for index in range(0, len(inputs), self._translation_batch_size)
        ]
        results = await _gather_fail_fast(
            *(
                self._director.direct_emotions(
                    performance_direction=performance_direction,
                    utterances=batch,
                )
                for batch in batches
            )
        )
        directions = tuple(item for result in results for item in result.items)
        expected_keys = tuple((item.utterance_id, item.revision) for item in inputs)
        direction_keys = tuple((item.utterance_id, item.revision) for item in directions)
        if (
            len(set(direction_keys)) != len(direction_keys)
            or set(direction_keys) != set(expected_keys)
        ):
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "emotion direction IDs or revisions do not match current utterances",
                retryable=False,
            )
        by_key = {(item.utterance_id, item.revision): item for item in directions}
        return tuple(
            by_key[key].model_copy(
                update={"emotion_vector": normalize_directed_vector(by_key[key].emotion_vector)}
            )
            for key in expected_keys
        )


_ResultT = TypeVar("_ResultT")


async def _gather_fail_fast(
    *awaitables: Awaitable[_ResultT],
) -> tuple[_ResultT, ...]:
    if not awaitables:
        return ()
    tasks: tuple[asyncio.Future[_ResultT], ...] = tuple(
        asyncio.ensure_future(awaitable) for awaitable in awaitables
    )
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
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
    if pending:
        await asyncio.gather(*pending)
    return tuple(task.result() for task in tasks)


def _materialize_cast(
    analyzed: tuple[AnalyzedUtterance, ...], cast: CastReconciliationResult
) -> tuple[tuple[CreateDirectorRole, ...], tuple[CreateDirectorUtterance, ...]]:
    if len(cast.assignments) != len(analyzed):
        raise _invalid_cast("cast must assign every analyzed utterance exactly once")
    role_by_key = {role.key: role for role in cast.roles}
    if len(role_by_key) != len(cast.roles):
        raise _invalid_cast("cast role keys must be unique")
    assignments = {item.utterance_index: item for item in cast.assignments}
    if set(assignments) != set(range(len(analyzed))):
        raise _invalid_cast("cast assignment indices are missing or duplicated")
    roles = tuple(
        CreateDirectorRole(
            canonical_name=role.canonical_name,
            kind=role.kind,
            aliases=role.aliases,
            confidence=role.confidence,
        )
        for role in cast.roles
    )
    utterances = []
    for ordinal, item in enumerate(analyzed):
        assignment = assignments[ordinal]
        role = role_by_key.get(assignment.role_key)
        if role is None:
            raise _invalid_cast("cast assignment refers to an unknown role")
        confirmed = assignment.confidence >= 0.8 and role.kind != "unknown"
        utterances.append(
            CreateDirectorUtterance(
                ordinal=ordinal,
                source_start=item.source_start,
                source_end=item.source_end,
                source_text=item.source_text,
                kind=item.kind,
                speak_enabled=item.speak_enabled,
                role_name=role.canonical_name,
                role_confidence=assignment.confidence,
                role_confirmed=confirmed,
            )
        )
    return roles, tuple(utterances)


def _invalid_cast(message: str) -> PipelineError:
    return PipelineError(
        ErrorCode.LLM_INVALID_RESPONSE,
        "llm",
        message,
        retryable=False,
    )


def _apply_directed_emotions(
    *,
    translations: tuple[TranslationResultItem, ...],
    directions: tuple[EmotionDirectionResultItem, ...],
    expected: tuple[TranslationInput, ...],
) -> tuple[TranslationResultItem, ...]:
    expected_keys = tuple((item.utterance_id, item.revision) for item in expected)
    translation_keys = tuple((item.utterance_id, item.revision) for item in translations)
    direction_keys = tuple((item.utterance_id, item.revision) for item in directions)
    if (
        len(set(expected_keys)) != len(expected_keys)
        or len(set(translation_keys)) != len(translation_keys)
        or len(set(direction_keys)) != len(direction_keys)
        or set(translation_keys) != set(expected_keys)
        or set(direction_keys) != set(expected_keys)
    ):
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "emotion direction IDs or revisions do not match translated utterances",
            retryable=False,
        )
    direction_by_key = {(item.utterance_id, item.revision): item for item in directions}
    return tuple(
        item.model_copy(
            update={
                "emotion_vector": normalize_directed_vector(
                    direction_by_key[(item.utterance_id, item.revision)].emotion_vector
                ),
                "speed_factor": direction_by_key[
                    (item.utterance_id, item.revision)
                ].speed_factor,
                "pause_after_ms": direction_by_key[
                    (item.utterance_id, item.revision)
                ].pause_after_ms,
            }
        )
        for item in translations
    )
