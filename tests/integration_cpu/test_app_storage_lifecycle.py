from __future__ import annotations

import re

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_health_exposes_durable_storage_dispatcher_and_quality_state(fake_settings) -> None:
    """A live control plane reports the durable components it has initialized."""
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    storage = payload["storage"]
    assert storage == {
        "status": "ready",
        "database_path": str(fake_settings.storage.database_path),
        "alembic_revision": "0001_batch2_foundation",
        "journal_mode": "wal",
        "quick_check": "ok",
        "artifact_root": str(fake_settings.storage.artifact_root),
        "missing_ready_versions": 0,
        "corrupt_ready_versions": 0,
        "last_recovery_run_id": storage["last_recovery_run_id"],
    }
    assert re.fullmatch(r"[0-9a-f-]{36}", storage["last_recovery_run_id"])
    assert payload["dispatcher"] == {
        "state": "running",
        "queued_count": 0,
        "active_job_id": None,
        "recovered_interrupted_count": 0,
    }
    assert payload["quality"]["mode"] == "fake"
    assert payload["quality"]["status"] == "ready"
    assert re.fullmatch(r"[0-9a-f]{64}", payload["quality"]["policy_fingerprint_sha256"])
