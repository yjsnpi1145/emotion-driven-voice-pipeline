from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient


async def _wait_status(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(200):
        payload = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


@pytest.mark.asyncio
async def test_cancelled_queued_job_is_never_started(fake_settings, request_json) -> None:
    app = create_app(
        fake_settings,
        index_client=FakeIndexTTSClient(delay_seconds=0.5),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/jobs/segment", json=request_json)
            first_id = first.json()["job_id"]
            for _ in range(100):
                if (await client.get(f"/api/v1/jobs/{first_id}")).json()["status"] == "running":
                    break
                await asyncio.sleep(0.01)

            second = await client.post("/api/v1/jobs/segment", json=request_json)
            second_id = second.json()["job_id"]
            cancelled = await client.post(f"/api/v1/jobs/{second_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

            assert (await _wait_status(client, first_id))["status"] == "succeeded"
            record = await _wait_status(client, second_id)

    assert record["status"] == "cancelled"
    assert not (fake_settings.runtime_dir / "jobs" / second_id).exists()


@pytest.mark.asyncio
async def test_frozen_retry_creates_a_distinct_durable_job(fake_settings, request_json) -> None:
    app = create_app(
        fake_settings,
        index_client=FakeIndexTTSClient(
            failure=PipelineError(ErrorCode.INDEX_ENGINE_ERROR, "index", "boom", retryable=True)
        ),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            original_submission = await client.post("/api/v1/jobs/segment", json=request_json)
            original_id = original_submission.json()["job_id"]
            original = await _wait_status(client, original_id)
            assert original["status"] == "failed"

            retried = await client.post(
                f"/api/v1/jobs/{original_id}/retry", json={"mode": "frozen_snapshot"}
            )
            assert retried.status_code == 202
            retry_id = retried.json()["job_id"]
            retry_record = (await client.get(f"/api/v1/jobs/{retry_id}")).json()

    assert retry_id != original_id
    assert retry_record["request_id"] == original["request_id"]
    assert retry_record["retry_of_job_id"] == original_id
    assert retry_record["attempt"] == original["attempt"] + 1
    assert retry_record["request_snapshot"] == original["request_snapshot"]


@pytest.mark.asyncio
async def test_running_cancel_waits_for_engine_abort_before_terminal(
    fake_settings, request_json
) -> None:
    app = create_app(
        fake_settings,
        index_client=FakeIndexTTSClient(delay_seconds=300.0),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            job_id = submitted.json()["job_id"]
            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] == "running":
                    break
                await asyncio.sleep(0.01)
            assert status["status"] == "running"

            response = await client.post(f"/api/v1/jobs/{job_id}/cancel")
            assert response.status_code == 202
            terminal = await _wait_status(client, job_id)

            health = app.state.plane.runtime.health()

    assert terminal["status"] == "cancelled"
    assert health.workers.indextts.active_inference == 0
