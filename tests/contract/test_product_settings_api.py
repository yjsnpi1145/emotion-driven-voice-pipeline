from __future__ import annotations

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_product_settings_and_local_paths_are_safe(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            initial = await client.get("/api/v1/settings/llm")
            updated = await client.put(
                "/api/v1/settings/llm",
                json={
                    "mode": "fake",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "ui-director",
                    "api_key": "must-not-be-returned",
                    "timeout_seconds": 30,
                    "max_retries": 1,
                    "max_reference_corrections": 3,
                },
            )
            tested = await client.post(
                "/api/v1/settings/llm/test",
                json={
                    "mode": "fake",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "candidate-only",
                    "timeout_seconds": 30,
                    "max_retries": 1,
                    "max_reference_corrections": 3,
                },
            )
            paths = await client.get("/api/v1/local/paths")
            invalid = await client.post(
                "/api/v1/local/open-folder", json={"resource": "arbitrary-shell-path"}
            )

    assert initial.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["model"] == "ui-director"
    assert updated.json()["api_key_configured"] is True
    assert "must-not-be-returned" not in updated.text
    assert "api_key" not in updated.json()
    assert tested.status_code == 200
    assert tested.json()["model"] == "candidate-only"
    assert paths.status_code == 200
    assert set(paths.json()) == {"model_library", "model_sources", "artifacts", "logs"}
    assert invalid.status_code == 422
