from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import _import_profile
from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_chapter_routes_submit_status_audio_and_timeline_without_path_leak(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "private-base.wav"
    write_tone(base_voice, 5.0)
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            submitted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "chapter",
                    "source_text": "第一句。第二句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice),
                    "model_profile_id": profile_id,
                },
            )
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]
            for _ in range(300):
                status = await client.get(f"/api/v1/chapters/{run_id}")
                assert status.status_code == 200
                if status.json()["status"] in {"succeeded", "failed", "interrupted"}:
                    break
                await asyncio.sleep(0.01)
            payload = status.json()
            audio = await client.get(f"/api/v1/chapters/{run_id}/audio")
            timeline = await client.get(f"/api/v1/chapters/{run_id}/timeline")

    assert payload["status"] == "succeeded"
    assert str(base_voice) not in str(payload)
    assert audio.status_code == 200
    assert timeline.status_code == 200
    assert len(timeline.json()["segments"]) == 2
