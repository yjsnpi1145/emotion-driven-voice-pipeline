from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import _import_profile
from voice_pipeline.api.app import create_app
from voice_pipeline.core.errors import ErrorCode, PipelineError


class _FailingAnalysis:
    def __init__(self, store) -> None:
        self._store = store

    async def analyze(self, project_id, *, expected_revision):
        await self._store.begin_analysis(project_id, expected_revision=expected_revision)
        raise PipelineError(
            ErrorCode.LLM_UNAVAILABLE,
            "llm",
            "director analysis fixture failed",
            retryable=True,
        )


@pytest.mark.asyncio
async def test_director_api_stages_analysis_review_and_translation(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/v1/director-projects",
                json={
                    "title": "双人场景",
                    "source_text": "旁白。\n甲：你好。\n乙：再见。",
                    "source_language": "zh",
                    "target_language": "ja",
                    "narration_enabled": True,
                },
            )
            assert created.status_code == 201
            project = created.json()
            submitted = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/analyze",
                json={"expected_revision": project["revision"]},
            )
            assert submitted.status_code == 202
            for _ in range(100):
                response = await client.get(f"/api/v1/director-projects/{project['project_id']}")
                project = response.json()
                if project["status"] == "role_review":
                    break
                await asyncio.sleep(0.01)
            assert project["status"] == "role_review"
            roles = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/roles")
            ).json()
            utterances = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/utterances")
            ).json()
            assert {role["canonical_name"] for role in roles} >= {"旁白", "甲", "乙"}
            source_text = "".join(item["source_text"] for item in utterances)
            assert source_text == "旁白。\n甲：你好。\n乙：再见。"
            assert all(item["working_text"] == item["source_text"] for item in utterances)

            edited_row = utterances[1]
            edited = await client.patch(
                f"/api/v1/director-utterances/{edited_row['utterance_id']}",
                json={
                    "expected_revision": edited_row["revision"],
                    "working_text": "甲：用户修改后的你好。",
                },
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["source_text"] == edited_row["source_text"]
            assert edited.json()["working_text"] == "甲：用户修改后的你好。"
            stale = await client.patch(
                f"/api/v1/director-utterances/{edited_row['utterance_id']}",
                json={
                    "expected_revision": edited_row["revision"],
                    "working_text": "甲：过期修改。",
                },
            )
            assert stale.status_code == 409
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()

            confirmed = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/confirm-roles",
                json={"expected_revision": project["revision"]},
            )
            assert confirmed.status_code == 200
            translating = confirmed.json()
            submitted = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/translate",
                json={"expected_revision": translating["revision"]},
            )
            assert submitted.status_code == 202
            for _ in range(100):
                project = (
                    await client.get(f"/api/v1/director-projects/{project['project_id']}")
                ).json()
                if project["status"] == "translation_review":
                    break
                await asyncio.sleep(0.01)
            assert project["status"] == "translation_review"
            translated_rows = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}/utterances"
                )
            ).json()
            spoken_row = next(item for item in translated_rows if item["speak_enabled"])
            recovered = await client.patch(
                f"/api/v1/director-utterances/{spoken_row['utterance_id']}",
                json={
                    "expected_revision": spoken_row["revision"],
                    "role_confirmed": True,
                },
            )
            assert recovered.status_code == 200, recovered.text
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            assert project["status"] == "role_review"
            health = await client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json()["director"] == {
                "active_analysis": 0,
                "active_generation": 0,
                "projects_needing_review": 1,
                "unavailable_role_presets": 0,
            }


@pytest.mark.asyncio
async def test_director_background_failure_is_persisted_for_retry(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        app.state.plane.director_analysis = _FailingAnalysis(app.state.plane.director_store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "失败可见",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            response = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/analyze",
                json={"expected_revision": project["revision"]},
            )
            assert response.status_code == 202
            for _ in range(100):
                project = (
                    await client.get(f"/api/v1/director-projects/{project['project_id']}")
                ).json()
                if project["last_error"] is not None:
                    break
                await asyncio.sleep(0.01)
            assert project["status"] == "analyzing"
            assert project["last_error"]["code"] == "LLM_UNAVAILABLE"
            assert project["last_error"]["retryable"] is True


@pytest.mark.asyncio
async def test_director_generation_uses_role_presets_and_completes(fake_settings, tmp_path) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    app = create_app(fake_settings)
    base_voice = tmp_path / "actor.wav"
    write_tone(base_voice, 15.0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "生成测试",
                        "source_text": "甲：你好。乙：再见。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            await client.post(
                f"/api/v1/director-projects/{project['project_id']}/analyze",
                json={"expected_revision": project["revision"]},
            )
            project = await _wait_status(client, project["project_id"], "role_review")
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
            project = await _wait_status(client, project["project_id"], "translation_review")
            project = (
                await client.post(
                    f"/api/v1/director-projects/{project['project_id']}/confirm-translation",
                    json={"expected_revision": project["revision"]},
                )
            ).json()
            preset = await client.post(
                "/api/v1/role-presets",
                json={
                    "name": "测试演员",
                    "base_voice_path": str(base_voice),
                    "model_profile_id": profile_id,
                    "default_speed": 1.25,
                },
            )
            assert preset.status_code == 201
            preset_id = preset.json()["preset_id"]
            roles = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}/roles")
            ).json()
            for role in roles:
                bound = await client.post(
                    f"/api/v1/director-roles/{role['role_id']}/preset",
                    json={"expected_revision": role["revision"], "preset_id": preset_id},
                )
                assert bound.status_code == 200
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            assert project["status"] == "ready"
            submitted = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/start-generation",
                json={"expected_revision": project["revision"]},
            )
            assert submitted.status_code == 202
            project = await _wait_status(client, project["project_id"], "succeeded", limit=500)
            assert project["status"] == "succeeded"
            generated_rows = await app.state.plane.director_store.list_utterances(
                project["project_id"]
            )
            generated_segments = [
                await app.state.plane.segment_store.get_segment(row.segment_id)
                for row in generated_rows
                if row.speak_enabled and row.segment_id is not None
            ]
            assert {segment.speed_factor for segment in generated_segments} == {1.25}
            assert "relative_path" not in str(project)
            assert project["audio_url"].endswith("/audio")
            progress = await client.get(
                f"/api/v1/director-projects/{project['project_id']}/progress"
            )
            assert progress.status_code == 200
            assert "snapshot" not in progress.json()["generation"]
            assert "relative_path" not in progress.text
            generation = await app.state.plane.director_store.current_generation(
                project["project_id"]
            )
            assert generation is not None
            items = await app.state.plane.director_store.list_generation_items(
                generation.generation_id
            )
            await app.state.plane.director_store.set_generation_item(
                generation.generation_id,
                items[0].utterance_id,
                status="failed",
                error={"code": "TEST_FAILURE"},
            )
            await app.state.plane.director_store.finish_generation(
                generation.generation_id,
                succeeded=False,
                error={"code": "TEST_FAILURE"},
            )
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            resumed = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/resume-generation",
                json={"expected_revision": project["revision"]},
            )
            assert resumed.status_code == 202
            project = await _wait_status(client, project["project_id"], "succeeded", limit=500)
            recomposed = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/recompose",
                json={"expected_revision": project["revision"]},
            )
            assert recomposed.status_code == 202, recomposed.text
            audio = await client.get(f"/api/v1/director-projects/{project['project_id']}/audio")
            assert audio.status_code == 200
            assert audio.content.startswith(b"RIFF")


async def _wait_status(client, project_id: str, expected: str, *, limit: int = 100):
    project = None
    for _ in range(limit):
        project = (await client.get(f"/api/v1/director-projects/{project_id}")).json()
        if project["status"] == expected:
            return project
        await asyncio.sleep(0.01)
    assert project is not None
    return project
