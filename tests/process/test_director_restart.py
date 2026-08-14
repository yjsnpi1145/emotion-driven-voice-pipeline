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
            project = (
                await client.post(
                    "/api/v1/director-projects",
                    json={
                        "title": "重启恢复",
                        "source_text": "旁白。",
                        "source_language": "zh",
                        "target_language": "ja",
                    },
                )
            ).json()
        await first.state.plane.director_store.begin_analysis(
            project["project_id"], expected_revision=project["revision"]
        )

    second = create_app(fake_settings)
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client:
            restored = (
                await client.get(f"/api/v1/director-projects/{project['project_id']}")
            ).json()

    assert restored["status"] == "analyzing"
    assert restored["last_error"]["code"] == "DIRECTOR_COMMAND_INTERRUPTED"
    assert restored["last_error"]["retryable"] is True
