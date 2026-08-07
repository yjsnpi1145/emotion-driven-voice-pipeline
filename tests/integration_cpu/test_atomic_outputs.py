from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_failed_gsv_never_publishes_target(
    external_settings, external_servers, tmp_path
) -> None:
    async with httpx.AsyncClient(base_url=external_servers[1].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "http500"})
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            from tests.integration_cpu.conftest import write_tone

            base = tmp_path / "base.wav"
            write_tone(base, 5.0)
            payload = {
                "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
                "base_voice_path": str(base.resolve()),
                "ref_text_cn": "我已经失去了一切，可我仍然活着。",
                "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
                "target_text": "私はまだ生きている。",
                "target_language": "ja",
                "seed": 1234,
            }
            resp = await client.post("/api/v1/jobs/segment", json=payload)
            job_id = resp.json()["job_id"]
            for _ in range(300):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert status["status"] == "failed"
            job_dir = external_settings.runtime_dir / "jobs" / job_id
            # target must not exist; reference must
            assert not (job_dir / "target.wav").exists()
            assert (job_dir / "reference.wav").exists()
            # reference manifest published before GSV
            assert (job_dir / "reference-manifest.json").exists()
    async with httpx.AsyncClient(base_url=external_servers[1].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "none"})


@pytest.mark.asyncio
async def test_output_sentinel_unchanged_after_engine_failure(
    external_settings, external_servers, tmp_path
) -> None:
    """CLI-level sentinel check is covered by the contract suite; here we
    verify the job directory never contains stray published partials after a
    failed engine call."""
    async with httpx.AsyncClient(base_url=external_servers[0].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "http500"})
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            from tests.integration_cpu.conftest import write_tone

            base = tmp_path / "base.wav"
            write_tone(base, 5.0)
            payload = {
                "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
                "base_voice_path": str(base.resolve()),
                "ref_text_cn": "我已经失去了一切，可我仍然活着。",
                "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
                "target_text": "私はまだ生きている。",
                "target_language": "ja",
                "seed": 1234,
            }
            resp = await client.post("/api/v1/jobs/segment", json=payload)
            job_id = resp.json()["job_id"]
            for _ in range(300):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert status["status"] == "failed"
            # no stray partial/working files in the job dir
            job_dir = external_settings.runtime_dir / "jobs" / job_id
            stray = [
                p.name for p in job_dir.iterdir() if "partial" in p.name or "working" in p.name
            ]
            assert stray == []
    async with httpx.AsyncClient(base_url=external_servers[0].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "none"})
