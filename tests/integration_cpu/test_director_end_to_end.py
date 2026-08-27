from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import _import_profile
from voice_pipeline.api.app import create_app
from voice_pipeline.modules.text.speakability import is_speakable_text


@pytest.mark.asyncio
async def test_director_end_to_end_uses_confirmed_preprocessing_and_filters_punctuation(
    fake_settings, tmp_path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    actor = tmp_path / "actor.wav"
    write_tone(actor, 12.0)
    app = create_app(fake_settings)
    source = (
        "“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”"
        "\n\n……\n\n甲：你好。\n乙：再见。"
    )
    edited_preprocessed = "“我的初吻……”她低声说，“祥子，为什么——”"
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
                        "source_text": source,
                        "source_language": "zh",
                        "target_language": "ja",
                        "preprocessing_mode": "structural",
                    },
                )
            ).json()
            started = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/preprocess",
                json={"expected_revision": project["revision"]},
            )
            assert started.status_code == 202
            project = await _wait_project(client, project["project_id"], "preprocess_review")
            page = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}/preprocess",
                    params={"offset": 0, "limit": 100},
                )
            ).json()
            paragraph = page["items"][0]
            edited = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}",
                json={
                    "expected_project_revision": project["revision"],
                    "expected_revision": paragraph["revision"],
                    "preprocessed_text": edited_preprocessed,
                },
            )
            assert edited.status_code == 200, edited.text
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            confirmed = await client.post(
                f"/api/v1/director-projects/{project['project_id']}"
                "/confirm-preprocessing",
                json={"expected_revision": project["revision"]},
            )
            assert confirmed.status_code == 202, confirmed.text
            project = await _wait_project(client, project["project_id"], "role_review")
            rows = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/utterances")
            ).json()
            stored_project = await app.state.plane.director_store.get_project(
                project["project_id"]
            )
            assert stored_project.source_text == source
            assert stored_project.preprocessed_text is not None
            assert stored_project.preprocessed_text.startswith(edited_preprocessed)
            assert "".join(row["source_text"] for row in rows) == (
                stored_project.preprocessed_text
            )
            bridge = next(row for row in rows if row["source_text"] == "她低声说，")
            assert bridge["kind"] == "narration"
            assert bridge["source_text"] not in {
                "“我的初吻……”",
                "“祥子，为什么——”",
            }
            punctuation_rows = [
                row for row in rows if not is_speakable_text(row["source_text"])
            ]
            assert punctuation_rows
            assert all(not row["speak_enabled"] for row in punctuation_rows)
            assert all(row["preprocess_paragraph_id"] for row in rows)

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
            translated_rows = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}/utterances"
                )
            ).json()
            assert all(
                row["synthesis_text"] and row["ref_text_cn"]
                for row in translated_rows
                if row["speak_enabled"]
            )
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
            spoken_role_ids = {
                row["role_id"] for row in translated_rows if row["speak_enabled"]
            }
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
            project = await _wait_project(client, project["project_id"], "succeeded", limit=1500)
            progress = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/progress")
            ).json()
            assert [item["ordinal"] for item in progress["items"]] == list(
                range(len(progress["items"]))
            )
            assert all(item["status"] == "ready" for item in progress["items"]), progress
            generated_rows = await app.state.plane.director_store.list_utterances(
                project["project_id"]
            )
            spoken_generated = [row for row in generated_rows if row.speak_enabled]
            assert spoken_generated
            assert all(row.segment_id is not None for row in spoken_generated)
            assert all(row.working_text.strip() for row in spoken_generated)
            for row in spoken_generated:
                segment = await app.state.plane.segment_store.get_segment(row.segment_id)
                assert segment.synthesis_text.strip()
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
    assert project["status"] == status, project
    return project
