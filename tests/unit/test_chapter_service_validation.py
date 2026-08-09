from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.conftest import write_tone
from voice_pipeline.core.chapter_service import ChapterService
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.chapter import ChapterSynthesisRequest
from voice_pipeline.models.model_profiles import ResolvedModelProfile
from voice_pipeline.modules.llm.models import DirectedSegment, DirectorPlan


class InvalidCoverageDirector:
    async def create_plan(self, *, source_text: str, target_language: str) -> DirectorPlan:
        del target_language
        return DirectorPlan(
            source_text_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            segments=(
                DirectedSegment(
                    ordinal=0,
                    source_start=0,
                    source_end=1,
                    emotion_description="calm",
                    emotion_vector=(0, 0, 0, 0, 0, 0, 0, 0.3),
                    synthesis_text="これは訳文です。",
                    ref_text_cn="我很平静。",
                    pause_after_ms=0,
                    speed_factor=1.0,
                    seed=1234,
                ),
            ),
        )

    async def correct_reference_text(self, **kwargs: object) -> str:
        raise AssertionError(f"correction must not run for an invalid plan: {kwargs}")


class RecordingQueue:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, operation: object, **kwargs: object) -> object:
        del operation, kwargs
        self.calls += 1
        raise AssertionError("GPU queue must not run for an invalid director plan")


@pytest.mark.asyncio
async def test_chapter_rejects_invalid_director_coverage_before_gpu_probe(tmp_path: Path) -> None:
    base_voice = (tmp_path / "base.wav").resolve()
    write_tone(base_voice, seconds=5.0)
    profile_id = uuid4()
    profile = ResolvedModelProfile(
        profile_id=profile_id,
        display_name="voice",
        gpt_relative_path="profiles/voice/GPT/model.ckpt",
        sovits_relative_path="profiles/voice/SoVITS/model.pth",
        gpt_sha256="1" * 64,
        sovits_sha256="2" * 64,
        gpt_path=(tmp_path / "model.ckpt").resolve(),
        sovits_path=(tmp_path / "model.pth").resolve(),
    )

    async def resolve_profile(_: object) -> ResolvedModelProfile:
        return profile

    queue = RecordingQueue()
    service = ChapterService(
        chapters=object(),  # type: ignore[arg-type]
        jobs=object(),  # type: ignore[arg-type]
        segment_jobs=object(),  # type: ignore[arg-type]
        versions=object(),  # type: ignore[arg-type]
        artifacts=object(),  # type: ignore[arg-type]
        model_profile_resolver=resolve_profile,  # type: ignore[arg-type]
        gsv_fingerprint=lambda: None,  # type: ignore[arg-type,return-value]
        director=InvalidCoverageDirector(),  # type: ignore[arg-type]
        synthesis=object(),  # type: ignore[arg-type]
        queue=queue,  # type: ignore[arg-type]
        jobs_root=tmp_path / "jobs",
        max_reference_corrections=2,
        notify_jobs=lambda: None,  # type: ignore[arg-type]
    )

    with pytest.raises(PipelineError) as exc_info:
        await service.submit(
            ChapterSynthesisRequest(
                request_id=uuid4(),
                title="invalid plan",
                source_text="甲乙",
                target_language="ja",
                base_voice_path=base_voice,
                model_profile_id=profile_id,
            )
        )

    assert exc_info.value.code == ErrorCode.LLM_INVALID_RESPONSE
    assert queue.calls == 0
