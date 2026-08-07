from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient


@pytest.mark.asyncio
async def test_invalid_emotion_vector_returns_422_and_creates_no_job(
    fake_settings, request_json
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            bad = dict(request_json)
            bad["emotion_vector"] = [0.2] * 8  # sum 1.6 > 0.8
            resp = await client.post("/api/v1/jobs/segment", json=bad)
            assert resp.status_code == 422
            payload = resp.json()
            assert payload["error"]["code"] == "INVALID_INPUT"
            # no job dirs were created
            jobs_root = fake_settings.runtime_dir / "jobs"
            assert not jobs_root.exists() or not any(jobs_root.iterdir())


@pytest.mark.asyncio
async def test_unknown_job_returns_404(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/jobs/11111111-2222-4333-8444-555555555555")
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_index_failure_fails_job_without_gsv(fake_settings, request_json) -> None:
    failing_index = FakeIndexTTSClient(
        failure=PipelineError(ErrorCode.INDEX_ENGINE_ERROR, "index", "boom", retryable=False)
    )
    app = create_app(fake_settings, index_client=failing_index)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            job_id = submitted.json()["job_id"]
            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert status["status"] == "failed"
            assert status["error"]["code"] == "INDEX_ENGINE_ERROR"
            # audio endpoints for a failed job return 409 with stored error
            audio = await client.get(f"/api/v1/jobs/{job_id}/audio/reference")
            assert audio.status_code == 409
            assert audio.json()["error"]["code"] == "INDEX_ENGINE_ERROR"


@pytest.mark.asyncio
async def test_audio_before_finish_returns_409(fake_settings, request_json) -> None:
    slow_index = FakeIndexTTSClient(delay_seconds=0.4)
    app = create_app(fake_settings, index_client=slow_index)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            job_id = submitted.json()["job_id"]
            # Immediately request audio while the job is queued/running.
            audio = await client.get(f"/api/v1/jobs/{job_id}/audio/reference")
            assert audio.status_code == 409
            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert status["status"] == "succeeded"


@pytest.mark.asyncio
async def test_shutdown_rejects_new_jobs_and_fails_pending(fake_settings, request_json) -> None:
    hanging_index = FakeIndexTTSClient(delay_seconds=300.0)
    app = create_app(fake_settings, index_client=hanging_index)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/jobs/segment", json=request_json)
            first_id = first.json()["job_id"]
            # Wait until the first job is running inside the queue.
            for _ in range(200):
                s1 = (await client.get(f"/api/v1/jobs/{first_id}")).json()
                if s1["status"] == "running":
                    break
                await asyncio.sleep(0.01)
            assert s1["status"] == "running"

            second = await client.post("/api/v1/jobs/segment", json=request_json)
            second_id = second.json()["job_id"]

            started = asyncio.get_running_loop().time()
            resp = await client.post("/api/v1/control/shutdown")
            assert resp.status_code == 200
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 5.5

            # New jobs are rejected while stopping.
            rejected = await client.post("/api/v1/jobs/segment", json=request_json)
            assert rejected.status_code == 503

            for job_id in (first_id, second_id):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                assert status["status"] == "failed"


@pytest.mark.asyncio
async def test_health_reports_ready_in_fake_mode(fake_settings) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["status"] == "ready"
            assert health["mode"] == "fake"
            assert health["engine_lifecycle"] == "resident"
            assert health["gpu_queue"]["max_concurrency"] == 1
            assert health["workers"]["indextts"]["state"] == "ready"
            assert health["workers"]["gpt_sovits"]["state"] == "ready"
            assert health["control"]["instance_id"]
            assert health["control"]["audit_log"]
