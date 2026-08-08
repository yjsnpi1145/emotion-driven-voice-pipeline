from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_import_list_activate_and_use_a_local_model_profile(
    fake_settings, request_json, tmp_path
) -> None:
    sources = tmp_path / "source-models"
    sources.mkdir()
    gpt = sources / "voice.ckpt"
    sovits = sources / "voice.pth"
    gpt.write_bytes(b"gpt-v1")
    sovits.write_bytes(b"sovits-v1")
    fake_settings.model_library.models_root = tmp_path / "model-library"
    fake_settings.model_library.allowed_import_roots = [sources]
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/v1/model-profiles/import",
                json={
                    "display_name": "voice-v1",
                    "gpt_source_path": str(gpt.resolve()),
                    "sovits_source_path": str(sovits.resolve()),
                },
            )
            assert created.status_code == 201
            profile_id = created.json()["profile_id"]
            activated = await client.post(f"/api/v1/model-profiles/{profile_id}/activate")
            assert activated.status_code == 200
            assert activated.json()["active"] is True
            listed = await client.get("/api/v1/model-profiles")
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            job_id = submitted.json()["job_id"]
            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)

    assert listed.status_code == 200
    assert [item["profile_id"] for item in listed.json()] == [profile_id]
    assert status["status"] == "succeeded"
    assert [str(item.profile_id) for item in app.state.plane.gsv.loaded_profiles] == [profile_id]
