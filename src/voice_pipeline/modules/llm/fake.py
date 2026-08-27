from __future__ import annotations

import hashlib
from uuid import UUID

from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    CandidateRoleAssignment,
    CastReconciliationResult,
    ChunkAnalysisResult,
    PreprocessRewriteItem,
    PreprocessRewriteResult,
    PreprocessRewriteUnit,
    ReconciledRole,
    ScriptChunk,
    ScriptTranslationResult,
    TranslationInput,
    TranslationResultItem,
)
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan


class FakeDirector:
    """A deterministic source-range director used by local CPU tests."""

    async def create_plan(
        self,
        *,
        source_text: str,
        target_language: LanguageCode,
        activity_id: UUID | None = None,
    ) -> DirectorPlan:
        del target_language, activity_id
        boundaries = _boundaries(source_text)
        return DirectorPlan(
            source_text_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            segments=tuple(
                DirectedSegment(
                    ordinal=ordinal,
                    source_start=start,
                    source_end=end,
                    emotion_description="平静、克制",
                    emotion_vector=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
                    synthesis_text=source_text[start:end],
                    ref_text_cn="我仍然保持冷静。",
                    pause_after_ms=500,
                    speed_factor=1.0,
                    seed=1234 + ordinal,
                )
                for ordinal, (start, end) in enumerate(boundaries)
            ),
        )

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: str,
        emotion_description: str,
        activity_id: UUID | None = None,
    ) -> str:
        del direction, emotion_description, activity_id
        return current

    async def analyze_script_chunk(
        self, *, chunk: ScriptChunk, activity_id: UUID | None = None
    ) -> ChunkAnalysisResult:
        del activity_id
        from voice_pipeline.modules.llm.script_chunking import build_analysis_units
        from voice_pipeline.modules.text.speakability import is_speakable_text

        rows: list[AnalyzedUtterance] = []
        for unit in build_analysis_units(chunk):
            text = unit.source_text
            stripped = text.strip()
            role_name: str | None = None
            kind = "narration"
            confidence = 0.98
            speak_enabled = is_speakable_text(text)
            if unit.context == "pause_marker":
                kind = "stage_direction"
                speak_enabled = False
            elif unit.context == "quoted_dialogue":
                role_name = "角色"
                kind = "dialogue"
                speak_enabled = True
            elif unit.context == "quote_bridge_narration":
                kind = "narration"
                speak_enabled = True
            elif stripped.startswith(("（", "(", "【", "[")):
                kind = "stage_direction"
                speak_enabled = False
            else:
                colon = next((stripped.find(mark) for mark in ("：", ":") if mark in stripped), -1)
                if 0 < colon <= 16:
                    candidate = stripped[:colon].strip()
                    if candidate and not any(mark in candidate for mark in "，。！？,.!?"):
                        role_name = candidate
                        kind = "dialogue"
                        confidence = 0.96
            rows.append(
                AnalyzedUtterance(
                    source_start=unit.source_start,
                    source_end=unit.source_end,
                    source_text=text,
                    kind=kind,  # type: ignore[arg-type]
                    temporary_role_name=role_name,
                    role_aliases=(),
                    role_confidence=confidence,
                    speak_enabled=speak_enabled,
                )
            )
        return ChunkAnalysisResult(utterances=tuple(rows))

    async def rewrite_preprocess_paragraph(
        self,
        *,
        paragraph_id: str,
        units: tuple[PreprocessRewriteUnit, ...],
        activity_id: UUID | None = None,
    ) -> PreprocessRewriteResult:
        del paragraph_id, activity_id
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

    async def reconcile_cast(
        self,
        *,
        utterances: tuple[AnalyzedUtterance, ...],
        activity_id: UUID | None = None,
    ) -> CastReconciliationResult:
        del activity_id
        names: list[str] = []
        if any(item.kind == "narration" for item in utterances):
            names.append("旁白")
        if any(item.kind == "stage_direction" for item in utterances):
            names.append("未分配")
        names.extend(
            dict.fromkeys(
                item.temporary_role_name
                for item in utterances
                if item.temporary_role_name is not None
            )
        )
        roles = tuple(
            ReconciledRole(
                key=f"role-{index}",
                canonical_name=name,
                kind=(
                    "narrator" if name == "旁白" else "unknown" if name == "未分配" else "character"
                ),
                aliases=(),
                confidence=0.98,
            )
            for index, name in enumerate(names)
        )
        key_by_name = {role.canonical_name: role.key for role in roles}
        assignments = []
        for index, item in enumerate(utterances):
            name = item.temporary_role_name or (
                "未分配" if item.kind == "stage_direction" else "旁白"
            )
            assignments.append(
                CandidateRoleAssignment(
                    utterance_index=index,
                    role_key=key_by_name[name],
                    confidence=item.role_confidence,
                )
            )
        return CastReconciliationResult(roles=roles, assignments=tuple(assignments))

    async def translate_utterances(
        self,
        *,
        target_language: LanguageCode,
        utterances: tuple[TranslationInput, ...],
        activity_id: UUID | None = None,
    ) -> ScriptTranslationResult:
        del activity_id
        return ScriptTranslationResult(
            items=tuple(
                TranslationResultItem(
                    utterance_id=item.utterance_id,
                    revision=item.revision,
                    synthesis_text=item.source_text,
                    ref_text_cn=(
                        item.source_text
                        if target_language in {"zh", "yue"} and _contains_han(item.source_text)
                        else "这是一句需要配音的台词。"
                    ),
                    emotion_vector=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3),
                    speed_factor=1.0,
                    pause_after_ms=400,
                )
                for item in utterances
            )
        )


def _boundaries(source_text: str) -> list[tuple[int, int]]:
    boundaries: list[tuple[int, int]] = []
    start = 0
    for index, character in enumerate(source_text, start=1):
        if character in "。！？.!?\n" or index - start >= 40:
            boundaries.append((start, index))
            start = index
    if start < len(source_text):
        boundaries.append((start, len(source_text)))
    return boundaries


def _director_boundaries(source_text: str) -> list[tuple[int, int]]:
    raw = _boundaries(source_text)
    merged: list[tuple[int, int]] = []
    pending_start: int | None = None
    for start, end in raw:
        if not source_text[start:end].strip():
            pending_start = start if pending_start is None else pending_start
            continue
        actual_start = pending_start if pending_start is not None else start
        merged.append((actual_start, end))
        pending_start = None
    if pending_start is not None and merged:
        previous_start, _ = merged[-1]
        merged[-1] = (previous_start, len(source_text))
    if not merged:
        merged.append((0, len(source_text)))
    return merged


def _contains_han(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)
