from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app


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
