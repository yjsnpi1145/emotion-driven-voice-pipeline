from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from uuid import UUID

from voice_pipeline.models.director import DirectorUtteranceRecord
from voice_pipeline.models.director_llm import EmotionContextUnit, EmotionDirectionInput
from voice_pipeline.models.schemas import EmotionVector, RawEmotionVector

_CALM_VECTOR: RawEmotionVector = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2)


def _context_unit(
    utterance: DirectorUtteranceRecord,
    role_names: Mapping[UUID, str],
) -> EmotionContextUnit:
    return EmotionContextUnit(
        ordinal=utterance.ordinal,
        role_name=role_names.get(utterance.role_id) if utterance.role_id else None,
        kind=utterance.kind,
        speak_enabled=utterance.speak_enabled,
        text=utterance.working_text,
    )


def build_emotion_inputs(
    *,
    utterances: Sequence[DirectorUtteranceRecord],
    role_names: Mapping[UUID, str],
    reviewed_source: str,
    context_chars: int = 600,
    neighbor_count: int = 3,
) -> tuple[EmotionDirectionInput, ...]:
    ordered = tuple(sorted(utterances, key=lambda item: item.ordinal))
    inputs: list[EmotionDirectionInput] = []
    for index, utterance in enumerate(ordered):
        if not utterance.speak_enabled:
            continue
        scene_start = max(0, utterance.source_start - context_chars)
        scene_end = min(len(reviewed_source), utterance.source_end + context_chars)
        scene_context = reviewed_source[scene_start:scene_end]
        if not scene_context.strip():
            scene_context = utterance.working_text
        previous = ordered[max(0, index - neighbor_count) : index]
        following = ordered[index + 1 : index + 1 + neighbor_count]
        inputs.append(
            EmotionDirectionInput(
                utterance_id=utterance.utterance_id,
                revision=utterance.revision,
                role_name=role_names.get(utterance.role_id) if utterance.role_id else None,
                source_text=utterance.working_text,
                scene_context=scene_context,
                previous_units=tuple(_context_unit(item, role_names) for item in previous),
                next_units=tuple(_context_unit(item, role_names) for item in following),
            )
        )
    return tuple(inputs)


def normalize_directed_vector(vector: RawEmotionVector) -> EmotionVector:
    if math.isclose(max(vector), min(vector), abs_tol=1e-9):
        return _CALM_VECTOR
    return vector
