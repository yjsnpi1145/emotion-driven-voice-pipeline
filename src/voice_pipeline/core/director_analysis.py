from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID

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
    ScriptChunk,
    ScriptTranslationResult,
    TranslationInput,
)
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.script_chunking import split_script, validate_chunk_analysis
from voice_pipeline.storage.director_store import DirectorStore


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
        chunks = split_script(project.source_text, self._max_chunk_chars)

        async def resolve(ordinal: int, chunk: ScriptChunk) -> ChunkAnalysisResult:
            cached = await self._store.load_analysis_chunk(project_id, chunk)
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
                llm_fingerprint="runtime-director-v1",
            )
            return result

        results = await asyncio.gather(
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
        spoken = [
            TranslationInput(
                utterance_id=item.utterance_id,
                revision=item.revision,
                source_text=item.source_text,
            )
            for item in await self._store.list_utterances(project_id)
            if item.speak_enabled
        ]
        batches = [
            tuple(spoken[index : index + self._translation_batch_size])
            for index in range(0, len(spoken), self._translation_batch_size)
        ]
        results = await asyncio.gather(
            *(
                self._director.translate_utterances(
                    target_language=project.target_language,
                    utterances=batch,
                )
                for batch in batches
            )
        )
        items = tuple(item for result in results for item in result.items)
        return await self._store.publish_translation(
            project_id,
            expected_revision=project.revision,
            items=items,
        )


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
