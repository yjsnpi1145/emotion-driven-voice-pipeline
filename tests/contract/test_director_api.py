from __future__ import annotations

import asyncio
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select, update

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import _import_profile
from voice_pipeline.api.app import create_app
from voice_pipeline.core.director_analysis import ScriptAnalysisService
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.storage.orm import director_edit_events, segments


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


class _BlockingPreprocessing:
    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def run(self, project_id, *, expected_revision):
        del project_id, expected_revision
        self.calls += 1
        await self.release.wait()


class _BlockingAnalysis:
    def __init__(self) -> None:
        self.analysis_calls = 0
        self.translation_calls = 0
        self.release = asyncio.Event()

    async def analyze(self, project_id, *, expected_revision):
        del project_id, expected_revision
        self.analysis_calls += 1
        await self.release.wait()

    async def translate(self, project_id, *, expected_revision):
        del project_id, expected_revision
        self.translation_calls += 1
        await self.release.wait()


async def _wait_project_status(client, project_id: str, expected: str):
    project = None
    for _ in range(200):
        project = (await client.get(f"/api/v1/director-projects/{project_id}")).json()
        if project["status"] == expected:
            return project
        await asyncio.sleep(0.01)
    assert project is not None
    return project


async def _preprocess_to_review(client, project: dict) -> dict:
    response = await client.post(
        f"/api/v1/director-projects/{project['project_id']}/preprocess",
        json={"expected_revision": project["revision"]},
    )
    assert response.status_code == 202, response.text
    return await _wait_project_status(client, project["project_id"], "preprocess_review")


async def _confirm_and_wait_for_roles(client, project: dict) -> dict:
    response = await client.post(
        f"/api/v1/director-projects/{project['project_id']}/confirm-preprocessing",
        json={"expected_revision": project["revision"]},
    )
    assert response.status_code == 202, response.text
    return await _wait_project_status(client, project["project_id"], "role_review")


@pytest.mark.asyncio
async def test_director_project_persists_revisioned_performance_direction(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/director-projects",
                json={
                    "title": "表演指导",
                    "source_text": "甲：我没事。",
                    "source_language": "zh",
                    "target_language": "ja",
                    "performance_direction": "  整体偏平静，避免夸张。  ",
                },
            )
            assert created.status_code == 201, created.text
            project = created.json()
            assert project["performance_direction"] == "整体偏平静，避免夸张。"

            too_long = await client.post(
                "/api/v1/director-projects",
                json={
                    "title": "过长指导",
                    "source_text": "甲：你好。",
                    "source_language": "zh",
                    "target_language": "ja",
                    "performance_direction": "静" * 2001,
                },
            )
            assert too_long.status_code == 422

            updated = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}/performance-direction",
                json={
                    "expected_revision": project["revision"],
                    "performance_direction": "  \n",
                    "reapply": False,
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["performance_direction"] is None
            assert updated.json()["revision"] == project["revision"] + 1
            async with app.state.plane.database.read_session() as session:
                audit = (
                    await session.execute(
                        select(director_edit_events.c.operation).where(
                            director_edit_events.c.project_id == project["project_id"]
                        )
                    )
                ).scalars().all()
            assert "performance_direction_updated" in audit

            stale = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}/performance-direction",
                json={
                    "expected_revision": project["revision"],
                    "performance_direction": "过期修改",
                    "reapply": False,
                },
            )
            assert stale.status_code == 409


@pytest.mark.asyncio
async def test_director_preprocessing_api_supports_review_edit_restore_and_confirm(
    fake_settings,
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "预处理 API",
                        "source_text": "“第一句。”她低头说，“第二句。”\n\n旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                        "preprocessing_mode": "rewrite",
                    },
                )
            ).json()
            started = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/preprocess",
                json={"expected_revision": project["revision"]},
            )
            assert started.status_code == 202
            project = await _wait_project_status(
                client,
                project["project_id"],
                "preprocess_review",
            )
            health = await client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json()["director"]["projects_needing_review"] == 1
            page_response = await client.get(
                f"/api/v1/director-projects/{project['project_id']}/preprocess",
                params={"offset": 0, "limit": 1},
            )
            assert page_response.status_code == 200
            page = page_response.json()
            assert page["total_count"] == 2
            assert len(page["items"]) == 1
            assert page["next_offset"] == 1
            paragraph = page["items"][0]

            blank = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}",
                json={
                    "expected_project_revision": project["revision"],
                    "expected_revision": paragraph["revision"],
                    "preprocessed_text": " \n",
                },
            )
            assert blank.status_code == 422
            edited = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}",
                json={
                    "expected_project_revision": project["revision"],
                    "expected_revision": paragraph["revision"],
                    "preprocessed_text": "用户校对后的句子。",
                },
            )
            assert edited.status_code == 200, edited.text
            paragraph = edited.json()
            project = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}"
                )
            ).json()
            stale = await client.patch(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}",
                json={
                    "expected_project_revision": 0,
                    "expected_revision": paragraph["revision"],
                    "preprocessed_text": "过期修改。",
                },
            )
            assert stale.status_code == 409
            restored = await client.post(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}/restore",
                json={
                    "expected_project_revision": project["revision"],
                    "expected_revision": paragraph["revision"],
                    "target": "structural",
                },
            )
            assert restored.status_code == 200, restored.text
            paragraph = restored.json()
            project = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}"
                )
            ).json()
            rewritten = await client.post(
                f"/api/v1/director-projects/{project['project_id']}"
                f"/preprocess-paragraphs/{paragraph['paragraph_id']}/rewrite",
                json={
                    "expected_project_revision": project["revision"],
                    "expected_revision": paragraph["revision"],
                },
            )
            assert rewritten.status_code == 200, rewritten.text
            project = (
                await client.get(
                    f"/api/v1/director-projects/{project['project_id']}"
                )
            ).json()
            confirmed = await client.post(
                f"/api/v1/director-projects/{project['project_id']}"
                "/confirm-preprocessing",
                json={"expected_revision": project["revision"]},
            )
            assert confirmed.status_code == 202, confirmed.text
            project = await _wait_project_status(
                client,
                project["project_id"],
                "role_review",
            )
            assert project["source_text"].startswith("“第一句。”")


@pytest.mark.asyncio
async def test_director_preprocess_command_is_single_flight(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        blocking = _BlockingPreprocessing()
        app.state.plane.director_preprocessing = blocking
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "单飞",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            path = f"/api/v1/director-projects/{project['project_id']}/preprocess"
            first, second = await asyncio.gather(
                client.post(path, json={"expected_revision": project["revision"]}),
                client.post(path, json={"expected_revision": project["revision"]}),
            )
            await asyncio.sleep(0)
            assert first.status_code == 202
            assert second.status_code == 202
            assert blocking.calls == 1
            blocking.release.set()


@pytest.mark.asyncio
async def test_director_analyze_rejects_unconfirmed_source(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "未确认预处理",
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

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "DIRECTOR_REVIEW_REQUIRED"


@pytest.mark.asyncio
async def test_confirm_preprocessing_double_submit_is_idempotent(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "确认单飞",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            project = await _preprocess_to_review(client, project)
            blocking = _BlockingAnalysis()
            app.state.plane.director_analysis = blocking
            path = (
                f"/api/v1/director-projects/{project['project_id']}"
                "/confirm-preprocessing"
            )

            first, second = await asyncio.gather(
                client.post(path, json={"expected_revision": project["revision"]}),
                client.post(path, json={"expected_revision": project["revision"]}),
            )
            await asyncio.sleep(0)

            assert (first.status_code, second.status_code) == (202, 202)
            assert blocking.analysis_calls == 1
            blocking.release.set()


@pytest.mark.asyncio
async def test_director_analysis_and_translation_commands_are_single_flight(
    fake_settings,
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            analysis_project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "分析单飞",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            blocking = _BlockingAnalysis()
            app.state.plane.director_analysis = blocking
            prepared = await app.state.plane.director_preprocessing.run(
                UUID(analysis_project["project_id"]),
                expected_revision=analysis_project["revision"],
            )
            prepared = await app.state.plane.director_store.confirm_preprocessing(
                prepared.project_id,
                expected_revision=prepared.revision,
            )
            analysis_project = (
                await client.get(
                    f"/api/v1/director-projects/{prepared.project_id}"
                )
            ).json()
            analyze_path = (
                f"/api/v1/director-projects/{analysis_project['project_id']}/analyze"
            )
            first, second = await asyncio.gather(
                client.post(
                    analyze_path,
                    json={"expected_revision": analysis_project["revision"]},
                ),
                client.post(
                    analyze_path,
                    json={"expected_revision": analysis_project["revision"]},
                ),
            )
            await asyncio.sleep(0)
            assert (first.status_code, second.status_code) == (202, 202)
            assert blocking.analysis_calls == 1
            blocking.release.set()
            await asyncio.sleep(0)

            app.state.plane.director_analysis = ScriptAnalysisService(
                app.state.plane.director_store,
                app.state.plane.llm_client,
            )
            translation_project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "翻译单飞",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            translation_project = await _preprocess_to_review(
                client, translation_project
            )
            translation_project = await _confirm_and_wait_for_roles(
                client, translation_project
            )
            blocking = _BlockingAnalysis()
            app.state.plane.director_analysis = blocking
            translate_path = (
                f"/api/v1/director-projects/{translation_project['project_id']}/translate"
            )
            first, second = await asyncio.gather(
                client.post(
                    translate_path,
                    json={"expected_revision": translation_project["revision"]},
                ),
                client.post(
                    translate_path,
                    json={"expected_revision": translation_project["revision"]},
                ),
            )
            await asyncio.sleep(0)
            assert (first.status_code, second.status_code) == (202, 202)
            assert blocking.translation_calls == 1
            blocking.release.set()


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
            project = await _preprocess_to_review(client, project)
            project = await _confirm_and_wait_for_roles(client, project)
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
            prepared = await app.state.plane.director_preprocessing.run(
                UUID(project["project_id"]),
                expected_revision=project["revision"],
            )
            prepared = await app.state.plane.director_store.confirm_preprocessing(
                prepared.project_id,
                expected_revision=prepared.revision,
            )
            app.state.plane.director_analysis = _FailingAnalysis(
                app.state.plane.director_store
            )
            response = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/analyze",
                json={"expected_revision": prepared.revision},
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
                        "source_text": "甲：你好。乙：再见。丙：晚安。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
            project = await _preprocess_to_review(client, project)
            project = await _confirm_and_wait_for_roles(client, project)
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
            skipped_role_id = roles[0]["role_id"]
            for index, role in enumerate(roles):
                bound = await client.post(
                    f"/api/v1/director-roles/{role['role_id']}/preset",
                    json=(
                        {
                            "expected_revision": role["revision"],
                            "mapping_mode": "skip",
                            "preset_id": None,
                        }
                        if index == 0
                        else {
                            "expected_revision": role["revision"],
                            "mapping_mode": "preset",
                            "preset_id": preset_id,
                        }
                    ),
                )
                assert bound.status_code == 200
                assert bound.json()["dubbing_enabled"] is (index != 0)
            project = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()
            assert project["status"] == "ready"
            pending_archive = await client.get(
                f"/api/v1/director-projects/{project['project_id']}/sentence-audio.zip"
            )
            assert pending_archive.status_code == 409
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
            skipped_rows = [row for row in generated_rows if str(row.role_id) == skipped_role_id]
            assert skipped_rows
            assert all(row.segment_id is None for row in skipped_rows)
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
            archive = await client.get(
                f"/api/v1/director-projects/{project['project_id']}/sentence-audio.zip"
            )
            assert archive.status_code == 200
            with ZipFile(BytesIO(archive.content)) as bundle:
                assert bundle.namelist()
                assert all(name.endswith(".wav") for name in bundle.namelist())

            items = await app.state.plane.director_store.list_generation_items(
                generation.generation_id
            )
            assert len(items) >= 2
            utterances = {
                row.utterance_id: row
                for row in await app.state.plane.director_store.list_utterances(
                    project["project_id"]
                )
            }
            missing = items[0]
            missing_segment_id = utterances[missing.utterance_id].segment_id
            assert missing_segment_id is not None
            async with app.state.plane.database.write_session() as session:
                await session.execute(
                    update(segments)
                    .where(segments.c.segment_id == str(missing_segment_id))
                    .values(active_gsv_version_id=None)
                )
            await app.state.plane.director_store.set_generation_item(
                generation.generation_id,
                missing.utterance_id,
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
            partial = await client.post(
                f"/api/v1/director-projects/{project['project_id']}/recompose",
                json={"expected_revision": project["revision"]},
            )
            assert partial.status_code == 202, partial.text
            partial_archive = await client.get(
                f"/api/v1/director-projects/{project['project_id']}/sentence-audio.zip"
            )
            assert partial_archive.status_code == 200
            with ZipFile(BytesIO(partial_archive.content)) as bundle:
                assert len(bundle.namelist()) == len(items) - 1


async def _wait_status(client, project_id: str, expected: str, *, limit: int = 100):
    project = None
    for _ in range(limit):
        project = (await client.get(f"/api/v1/director-projects/{project_id}")).json()
        if project["status"] == expected:
            return project
        await asyncio.sleep(0.01)
    assert project is not None
    return project
