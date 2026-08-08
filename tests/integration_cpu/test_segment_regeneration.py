from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.api.app import create_app


async def _profile(client: httpx.AsyncClient, tmp_path: Path) -> str:
    models = tmp_path / "models"
    models.mkdir()
    gpt, sovits = models / "voice.ckpt", models / "voice.pth"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    created = await client.post(
        "/api/v1/model-profiles/import",
        json={
            "display_name": "regeneration-voice",
            "gpt_source_path": str(gpt.resolve()),
            "sovits_source_path": str(sovits.resolve()),
        },
    )
    assert created.status_code == 201
    profile_id = created.json()["profile_id"]
    assert (await client.post(f"/api/v1/model-profiles/{profile_id}/activate")).status_code == 200
    return profile_id


async def _wait(client: httpx.AsyncClient, path: str) -> dict[str, object]:
    for _ in range(400):
        response = await client.get(path)
        assert response.status_code == 200
        value = response.json()
        if value["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"did not finish: {path}")


@pytest.mark.asyncio
async def test_explicit_regeneration_keeps_or_replaces_only_the_selected_artifact(
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
            profile_id = await _profile(client, tmp_path)
            chapter = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "regeneration",
                    "source_text": "第一句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice.resolve()),
                    "model_profile_id": profile_id,
                },
            )
            assert chapter.status_code == 202
            run_id = chapter.json()["run_id"]
            assert (await _wait(client, f"/api/v1/chapters/{run_id}"))["status"] == "succeeded"
            progress = (await client.get(f"/api/v1/chapters/{run_id}/progress")).json()["segments"][
                0
            ]
            segment_id = progress["segment_id"]
            original_ref = progress["active_ref_version_id"]
            original_gsv = progress["active_gsv_version_id"]

            gsv = await client.post(
                f"/api/v1/segments/{segment_id}/regenerate-gsv",
                json={"request_id": str(uuid4()), "model_profile_id": profile_id},
            )
            assert gsv.status_code == 202
            assert (await _wait(client, gsv.json()["status_url"]))["status"] == "succeeded"
            after_gsv = (await client.get(f"/api/v1/segments/{segment_id}")).json()
            assert after_gsv["active_ref_version_id"] == original_ref
            assert after_gsv["active_gsv_version_id"] != original_gsv

            jobs_before_reference = await app.state.plane.database.scalar_int(
                "SELECT count(*) FROM generation_jobs WHERE kind = 'gsv'"
            )
            reference = await client.post(
                f"/api/v1/segments/{segment_id}/regenerate-reference",
                json={"request_id": str(uuid4()), "base_voice_path": str(base_voice.resolve())},
            )
            assert reference.status_code == 202
            assert (await _wait(client, reference.json()["status_url"]))["status"] == "succeeded"
            after_reference = (await client.get(f"/api/v1/segments/{segment_id}")).json()
            assert after_reference["active_ref_version_id"] != original_ref
            assert after_reference["active_gsv_version_id"] == after_gsv["active_gsv_version_id"]
            assert (
                await app.state.plane.database.scalar_int(
                    "SELECT count(*) FROM generation_jobs WHERE kind = 'gsv'"
                )
                == jobs_before_reference
            )

            both = await client.post(
                f"/api/v1/segments/{segment_id}/regenerate-both",
                json={
                    "request_id": str(uuid4()),
                    "base_voice_path": str(base_voice.resolve()),
                    "model_profile_id": profile_id,
                },
            )
            assert both.status_code == 202
            assert (await _wait(client, both.json()["status_url"]))["status"] == "succeeded"
            for _ in range(400):
                current = await client.get(f"/api/v1/segments/{segment_id}")
                assert current.status_code == 200
                if (
                    current.json()["active_gsv_version_id"]
                    != after_reference["active_gsv_version_id"]
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("both regeneration did not activate a new GSV version")
