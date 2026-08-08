from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.api.app import create_app
from voice_pipeline.models.chapter import ChapterSynthesisRequest


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
