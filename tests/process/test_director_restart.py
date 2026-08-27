from __future__ import annotations

import httpx
import pytest

from voice_pipeline.api.app import create_app

pytest_plugins = ("tests.integration_cpu.conftest",)


@pytest.mark.asyncio
async def test_restart_marks_interrupted_director_command_retryable(
    fake_settings,
) -> None:
    first = create_app(fake_settings)
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            projects = []
            for status in ("preprocessing", "analyzing", "translating"):
                project = (
                    await client.post(
                        "/api/v1/director-projects",
                        json={
                            "title": f"重启恢复 {status}",
                            "source_text": "旁白。",
                            "source_language": "zh",
                            "target_language": "ja",
                        },
                    )
                ).json()
                projects.append(project)
        store = first.state.plane.director_store
        await store.begin_preprocessing(
            projects[0]["project_id"],
            expected_revision=projects[0]["revision"],
        )
        await store.begin_analysis(
            projects[1]["project_id"],
            expected_revision=projects[1]["revision"],
        )
        await store.begin_analysis(
            projects[2]["project_id"],
            expected_revision=projects[2]["revision"],
        )
        translating = await store.get_project(projects[2]["project_id"])
        await store._update_project_state(
            translating.project_id,
            expected_revision=translating.revision,
            allowed={"analyzing"},
            status="translating",
            event="fixture_translation_started",
        )

    second = create_app(fake_settings)
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            restored = [
                (
                    await client.get(
                        f"/api/v1/director-projects/{project['project_id']}"
                    )
                ).json()
                for project in projects
            ]

    assert [project["status"] for project in restored] == [
        "preprocessing",
        "analyzing",
        "translating",
    ]
    assert all(
        project["last_error"]["code"] == "DIRECTOR_COMMAND_INTERRUPTED"
        for project in restored
    )
    assert all(project["last_error"]["retryable"] is True for project in restored)
