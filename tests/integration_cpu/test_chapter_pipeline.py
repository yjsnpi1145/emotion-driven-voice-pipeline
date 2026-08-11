from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.api.app import create_app
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.chapter import ChapterSynthesisRequest
from voice_pipeline.models.persistence import SegmentReferenceRegenerationRequest
from voice_pipeline.modules.quality.fake import DeterministicQualityAnalyzer


class FailSecondQualityAnalyzer:
    """Reject the second persisted reference while leaving duration probes alone."""

    def __init__(self) -> None:
        self._delegate = DeterministicQualityAnalyzer()
        self.calls = 0

    @property
    def policy_fingerprint(self) -> str:
        return self._delegate.policy_fingerprint

    async def analyze_reference(self, *, audio_path, expected_text):
        self.calls += 1
        if self.calls == 2:
            raise PipelineError(
                ErrorCode.QUALITY_TEXT_MISMATCH,
                "quality",
                "injected reference quality failure",
                retryable=False,
            )
        return await self._delegate.analyze_reference(
            audio_path=audio_path, expected_text=expected_text
        )


async def _import_profile(client: httpx.AsyncClient, tmp_path: Path) -> str:
    source = tmp_path / "models"
    source.mkdir()
    gpt = source / "voice.ckpt"
    sovits = source / "voice.pth"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    created = await client.post(
        "/api/v1/model-profiles/import",
        json={
            "display_name": "chapter-voice",
            "gpt_source_path": str(gpt.resolve()),
            "sovits_source_path": str(sovits.resolve()),
        },
    )
    assert created.status_code == 201
    profile_id = created.json()["profile_id"]
    assert (await client.post(f"/api/v1/model-profiles/{profile_id}/activate")).status_code == 200
    return profile_id


@pytest.mark.asyncio
async def test_chapter_service_generates_segment_versions_and_final(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
        accepted = await app.state.plane.chapter_service.submit(
            ChapterSynthesisRequest(
                request_id=uuid4(),
                title="chapter",
                source_text="第一句。第二句。",
                target_language="ja",
                base_voice_path=base_voice,
                model_profile_id=profile_id,
            )
        )
        for _ in range(300):
            run = await app.state.plane.chapter_service.get(accepted.run_id)
            if run.status in {"succeeded", "failed", "interrupted"}:
                break
            await asyncio.sleep(0.01)

    assert run.status == "succeeded"
    assert run.final_audio is not None
    assert run.timeline is not None
    assert len(run.timeline.segments) == 2


@pytest.mark.asyncio
async def test_chapter_resume_reuses_ready_prefix_and_repaired_reference(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    quality = FailSecondQualityAnalyzer()
    app = create_app(fake_settings)
    app.state.plane.configure_quality(quality)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
        accepted = await app.state.plane.chapter_service.submit(
            ChapterSynthesisRequest(
                request_id=uuid4(),
                title="resumable chapter",
                source_text="第一句。第二句。第三句。",
                target_language="ja",
                base_voice_path=base_voice,
                model_profile_id=profile_id,
            )
        )
        for _ in range(400):
            failed = await app.state.plane.chapter_service.get(accepted.run_id)
            if failed.status == "failed":
                break
            await asyncio.sleep(0.01)
        assert failed.status == "failed"
        failed_progress = await app.state.plane.chapter_store.progress(accepted.run_id)
        first_gsv_version_id = failed_progress[0].active_gsv_version_id
        assert first_gsv_version_id is not None
        assert failed_progress[1].reference_job_status == "failed"

        repaired = await app.state.plane.regeneration.submit_reference(
            failed_progress[1].segment_id,
            SegmentReferenceRegenerationRequest(request_id=uuid4()),
        )
        for _ in range(400):
            repaired_job = await app.state.plane.registry.get(repaired.job_id)
            if repaired_job.status in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert repaired_job.status == "succeeded"
        repaired_progress = await app.state.plane.chapter_store.progress(accepted.run_id)
        repaired_reference_id = repaired_progress[1].active_ref_version_id
        assert repaired_reference_id is not None

        resumed = await app.state.plane.chapter_service.resume(accepted.run_id)
        assert resumed.status == "running"
        for _ in range(500):
            completed = await app.state.plane.chapter_service.get(accepted.run_id)
            if completed.status in {"succeeded", "failed", "interrupted"}:
                break
            await asyncio.sleep(0.01)

        assert completed.status == "succeeded"
        completed_progress = await app.state.plane.chapter_store.progress(accepted.run_id)
        assert completed_progress[0].active_gsv_version_id == first_gsv_version_id
        assert completed_progress[1].active_ref_version_id == repaired_reference_id
        assert all(item.gsv_state == "ready" for item in completed_progress)
        assert app.state.plane.index.calls == 3
