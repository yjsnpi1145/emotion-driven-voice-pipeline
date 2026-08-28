from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from voice_pipeline.core.director_generation import DirectorGenerationService
from voice_pipeline.models.director import DirectorUtteranceRecord


def _utterance(
    *, project_id: UUID, ordinal: int, segment_id: UUID | None
) -> DirectorUtteranceRecord:
    text = f"第{ordinal + 1}句。"
    return DirectorUtteranceRecord(
        utterance_id=uuid4(),
        project_id=project_id,
        ordinal=ordinal,
        source_start=ordinal * len(text),
        source_end=(ordinal + 1) * len(text),
        source_text=text,
        working_text=text,
        kind="dialogue",
        speak_enabled=True,
        role_id=uuid4(),
        role_confidence=1.0,
        role_confirmed=True,
        synthesis_text=text,
        ref_text_cn="这是自然的中文参考台词。",
        emotion_vector=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2),
        speed_factor=1.0,
        pause_after_ms=300,
        seed=1000 + ordinal,
        revision=0,
        task_id=uuid4() if segment_id is not None else None,
        segment_id=segment_id,
    )


class _DirectorRows:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.attached: list[tuple[UUID, UUID, UUID]] = []

    async def list_utterances(self, project_id):
        del project_id
        return self.rows

    async def attach_materialized_segment(self, utterance_id, *, task_id, segment_id):
        self.attached.append((utterance_id, task_id, segment_id))


class _Segments:
    def __init__(self) -> None:
        self.created = []
        self.task_id = uuid4()

    async def create_task(self, request):
        del request
        return SimpleNamespace(task_id=self.task_id)

    async def create_segment(self, task_id, request):
        assert task_id == self.task_id
        self.created.append(request)
        return SimpleNamespace(segment_id=uuid4())


@pytest.mark.asyncio
async def test_materialize_excludes_stale_skipped_segments_and_fills_only_missing_rows() -> None:
    project_id = uuid4()
    stale = _utterance(project_id=project_id, ordinal=0, segment_id=uuid4())
    existing = _utterance(project_id=project_id, ordinal=1, segment_id=uuid4())
    missing = _utterance(project_id=project_id, ordinal=2, segment_id=None)
    directors = _DirectorRows([stale, existing, missing])
    segments = _Segments()
    service = object.__new__(DirectorGenerationService)
    service._directors = directors
    service._segments = segments
    project = SimpleNamespace(
        project_id=project_id,
        title="部分恢复",
        source_text=existing.source_text + missing.source_text,
        preprocessed_text=None,
        target_language="ja",
    )

    mapping = await service._materialize(project, [existing, missing])

    assert set(mapping) == {existing.utterance_id, missing.utterance_id}
    assert mapping[existing.utterance_id] == existing.segment_id
    assert stale.utterance_id not in mapping
    assert [request.source_text for request in segments.created] == [missing.source_text]
    assert [item[0] for item in directors.attached] == [missing.utterance_id]
