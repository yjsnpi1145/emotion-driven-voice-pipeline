from __future__ import annotations

from uuid import UUID, uuid4

from voice_pipeline.core.contextual_emotion import (
    build_emotion_inputs,
    normalize_directed_vector,
)
from voice_pipeline.models.director import DirectorUtteranceRecord

PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")


def _utterance(
    *,
    ordinal: int,
    source_start: int,
    source_text: str,
    kind: str,
    speak_enabled: bool,
    role_id: UUID | None = None,
) -> DirectorUtteranceRecord:
    return DirectorUtteranceRecord(
        utterance_id=uuid4(),
        project_id=PROJECT_ID,
        ordinal=ordinal,
        source_start=source_start,
        source_end=source_start + len(source_text),
        source_text=source_text,
        working_text=source_text,
        kind=kind,
        speak_enabled=speak_enabled,
        role_id=role_id,
        role_confidence=1.0,
        role_confirmed=True,
        revision=ordinal + 1,
    )


def test_build_emotion_inputs_keeps_scene_speaker_and_nonspoken_neighbors() -> None:
    actor = uuid4()
    source = (
        "她收到噩耗，却强忍泪水。\n甲：\u201c我没事。\u201d\n"
        "（她攥紧信纸）\n乙：\u201c真的吗？\u201d"
    )
    narration = _utterance(
        ordinal=0,
        source_start=0,
        source_text="她收到噩耗，却强忍泪水。",
        kind="narration",
        speak_enabled=False,
    )
    first_line_start = source.index("我没事")
    first_line = _utterance(
        ordinal=1,
        source_start=first_line_start,
        source_text="我没事。",
        kind="dialogue",
        speak_enabled=True,
        role_id=actor,
    )
    direction = _utterance(
        ordinal=2,
        source_start=source.index("她攥紧信纸"),
        source_text="她攥紧信纸",
        kind="stage_direction",
        speak_enabled=False,
        role_id=actor,
    )
    reply = _utterance(
        ordinal=3,
        source_start=source.index("真的吗"),
        source_text="真的吗？",
        kind="dialogue",
        speak_enabled=True,
    )

    inputs = build_emotion_inputs(
        utterances=(narration, first_line, direction, reply),
        role_names={actor: "甲"},
        reviewed_source=source,
    )

    assert [item.utterance_id for item in inputs] == [
        first_line.utterance_id,
        reply.utterance_id,
    ]
    assert inputs[0].role_name == "甲"
    assert inputs[0].scene_context == source
    assert [unit.text for unit in inputs[0].previous_units] == [narration.working_text]
    assert [unit.text for unit in inputs[0].next_units] == [
        direction.working_text,
        reply.working_text,
    ]
    assert inputs[0].next_units[0].kind == "stage_direction"
    assert inputs[0].next_units[0].speak_enabled is False


def test_build_emotion_inputs_limits_timeline_and_scene_context() -> None:
    utterances = tuple(
        _utterance(
            ordinal=index,
            source_start=index,
            source_text=str(index),
            kind="dialogue",
            speak_enabled=True,
        )
        for index in range(8)
    )

    inputs = build_emotion_inputs(
        utterances=utterances,
        role_names={},
        reviewed_source="01234567",
        context_chars=2,
        neighbor_count=3,
    )

    assert inputs[4].scene_context == "23456"
    assert [unit.ordinal for unit in inputs[4].previous_units] == [1, 2, 3]
    assert [unit.ordinal for unit in inputs[4].next_units] == [5, 6, 7]


def test_normalize_directed_vector_replaces_semantically_empty_uniform_vectors() -> None:
    calm = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2)

    assert normalize_directed_vector((0.0,) * 8) == calm
    assert normalize_directed_vector((0.1,) * 8) == calm


def test_normalize_directed_vector_preserves_meaningful_vector() -> None:
    vector = (0.0, 0.0, 0.35, 0.0, 0.0, 0.2, 0.0, 0.1)

    assert normalize_directed_vector(vector) == vector
