from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import _import_profile
from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_director_end_to_end_keeps_source_order_and_can_exclude_narration(
    fake_settings, tmp_path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    actor = tmp_path / "actor.wav"
    write_tone(actor, 12.0)
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "导演验收",
                        "source_text": "旁白。\n甲：你好。\n乙：再见。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            await client.post(
                f"/api/v1/director-projects/{project['project_id']}/analyze",
                json={"expected_revision": project["revision"]},
            )
            project = await _wait_project(client, project["project_id"], "role_review")
            project = (
                await client.patch(
                    f"/api/v1/director-projects/{project['project_id']}/narration",
                    json={"expected_revision": project["revision"], "enabled": False},
                )
            ).json()
            rows = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/utterances")
            ).json()
            assert "".join(row["source_text"] for row in rows) == project["source_text"]
            assert all(not row["speak_enabled"] for row in rows if row["kind"] == "narration")
            edited_row = next(row for row in rows if row["kind"] == "dialogue")
            edited_text = "甲：用户修改后才进入配音的台词。"
            edit_response = await client.patch(
                f"/api/v1/director-utterances/{edited_row['utterance_id']}",
                json={
                    "expected_revision": edited_row["revision"],
                    "working_text": edited_text,
                },
            )
            assert edit_response.status_code == 200, edit_response.text
            assert edit_response.json()["source_text"] == edited_row["source_text"]
            assert edit_response.json()["working_text"] == edited_text
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            rows = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/utterances")
            ).json()

            project = (
                await client.post(
                    f"/api/v1/director-projects/{project['project_id']}/confirm-roles",
                    json={"expected_revision": project["revision"]},
                )
            ).json()
            await client.post(
                f"/api/v1/director-projects/{project['project_id']}/translate",
                json={"expected_revision": project["revision"]},
            )
            project = await _wait_project(client, project["project_id"], "translation_review")
            project = (
                await client.post(
                    f"/api/v1/director-projects/{project['project_id']}/confirm-translation",
                    json={"expected_revision": project["revision"]},
                )
            ).json()
            preset = (
                await client.post(
                    "/api/v1/role-presets",
                    json={
                        "name": "验收演员",
                        "base_voice_path": str(actor),
                        "model_profile_id": profile_id,
                        "default_speed": 1.0,
                    },
                )
            ).json()
            roles = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/roles")
            ).json()
            spoken_role_ids = {row["role_id"] for row in rows if row["speak_enabled"]}
            for role in roles:
                if role["role_id"] in spoken_role_ids:
                    response = await client.post(
                        f"/api/v1/director-roles/{role['role_id']}/preset",
                        json={
                            "expected_revision": role["revision"],
                            "preset_id": preset["preset_id"],
                        },
                    )
                    assert response.status_code == 200
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            response = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/start-generation",
                json={"expected_revision": project["revision"]},
            )
            assert response.status_code == 202
            project = await _wait_project(client, project["project_id"], "succeeded", limit=500)
            progress = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/progress")
            ).json()
            assert [item["ordinal"] for item in progress["items"]] == list(
                range(len(progress["items"]))
            )
            assert all(item["status"] == "ready" for item in progress["items"])
            generated = await app.state.plane.director_store.get_utterance(
                edit_response.json()["utterance_id"]
            )
            assert generated.source_text == edited_row["source_text"]
            assert generated.working_text == edited_text
            assert generated.synthesis_text == edited_text
            assert generated.segment_id is not None
            segment = await app.state.plane.segment_store.get_segment(generated.segment_id)
            assert segment.source_text == edited_row["source_text"]
            assert segment.synthesis_text == edited_text
            audio = await client.get(f"/api/v1/director-projects/{project['project_id']}/audio")
            assert audio.status_code == 200
            assert audio.content.startswith(b"RIFF")


async def _wait_project(client, project_id: str, status: str, *, limit: int = 100):
    project = None
    for _ in range(limit):
        project = (await client.get(f"/api/v1/director-projects/{project_id}")).json()
        if project["status"] == status:
            return project
        await asyncio.sleep(0.01)
    assert project is not None
    return project
