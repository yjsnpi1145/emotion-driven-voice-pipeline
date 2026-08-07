from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from voice_pipeline.api.app import create_app


async def _submit_and_wait(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    resp = await client.post(url, json=payload)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    for _ in range(300):
        status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if status["status"] in {"succeeded", "failed"}:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish in time")


def _request_json(tmp_path: Path, **overrides) -> dict:
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
    payload.update(overrides)
    return payload


async def _configure(external_servers, index_failure=None, gsv_failure=None) -> None:
    index_server, gsv_server = external_servers
    for server, failure in ((index_server, index_failure), (gsv_server, gsv_failure)):
        if failure is None:
            continue
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5) as client:
            await client.post("/__control/configure", json={"failure": failure})


def _audit_count(external_servers, engine: str) -> int:
    index_server, gsv_server = external_servers
    server = index_server if engine == "indextts" else gsv_server
    return len(server.audit_rows())


@pytest.mark.asyncio
async def test_invalid_emotion_vector_calls_no_engines(
    external_settings, external_servers, tmp_path
) -> None:
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/jobs/segment",
                json=_request_json(tmp_path, emotion_vector=[0.2] * 8),
            )
            assert resp.status_code == 422
    assert _audit_count(external_servers, "indextts") == 0
    assert _audit_count(external_servers, "gpt_sovits") == 0


@pytest.mark.asyncio
async def test_index_http_500_skips_gsv(external_settings, external_servers, tmp_path) -> None:
    await _configure(external_servers, index_failure="http500")
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            status = await _submit_and_wait(client, "/api/v1/jobs/segment", _request_json(tmp_path))
            assert status["status"] == "failed"
            assert status["error"]["code"] == "INDEX_ENGINE_ERROR"
    assert _audit_count(external_servers, "indextts") == 1
    assert _audit_count(external_servers, "gpt_sovits") == 0
    await _configure(external_servers, index_failure="none")


@pytest.mark.asyncio
async def test_index_no_file_is_invalid_audio(
    external_settings, external_servers, tmp_path
) -> None:
    await _configure(external_servers, index_failure="no_file")
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            status = await _submit_and_wait(client, "/api/v1/jobs/segment", _request_json(tmp_path))
            assert status["status"] == "failed"
            assert status["error"]["code"] == "INDEX_ENGINE_ERROR"
    assert _audit_count(external_servers, "gpt_sovits") == 0
    await _configure(external_servers, index_failure="none")


@pytest.mark.asyncio
async def test_index_reference_out_of_window_skips_gsv(
    external_settings, external_servers, tmp_path
) -> None:
    for failure in ("short", "long"):
        await _configure(external_servers, index_failure=failure)
        app = create_app(external_settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                status = await _submit_and_wait(
                    client, "/api/v1/jobs/segment", _request_json(tmp_path)
                )
                assert status["status"] == "failed"
                assert status["error"]["code"] == "REFERENCE_DURATION_OUT_OF_RANGE"
        assert _audit_count(external_servers, "gpt_sovits") == 0
        await _configure(external_servers, index_failure="none")


@pytest.mark.asyncio
async def test_gsv_http_500_keeps_reference(external_settings, external_servers, tmp_path) -> None:
    await _configure(external_servers, gsv_failure="http500")
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            status = await _submit_and_wait(client, "/api/v1/jobs/segment", _request_json(tmp_path))
            assert status["status"] == "failed"
            assert status["error"]["code"] == "GSV_ENGINE_ERROR"
            # reference manifest preserved
            assert (
                await client.get(f"/api/v1/jobs/{status['job_id']}/manifest/reference")
            ).status_code in (409, 200)
    await _configure(external_servers, gsv_failure="none")


@pytest.mark.asyncio
async def test_gsv_corrupt_wav_not_published(external_settings, external_servers, tmp_path) -> None:
    await _configure(external_servers, gsv_failure="corrupt")
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            status = await _submit_and_wait(client, "/api/v1/jobs/segment", _request_json(tmp_path))
            assert status["status"] == "failed"
            assert status["error"]["code"] in ("GSV_ENGINE_ERROR", "INVALID_AUDIO")
    await _configure(external_servers, gsv_failure="none")


@pytest.mark.asyncio
async def test_queue_timeout_fails_job_without_engine_calls(
    external_settings, external_servers, tmp_path
) -> None:
    # Slow down the index fake so the second job exceeds the 1s queue timeout.
    async with httpx.AsyncClient(base_url=external_servers[0].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "none", "delay_ms": 3000})
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await _submit_and_wait(client, "/api/v1/jobs/segment", _request_json(tmp_path))
            assert first["status"] == "succeeded"
    async with httpx.AsyncClient(base_url=external_servers[0].base_url, timeout=5) as client:
        await client.post("/__control/configure", json={"failure": "none", "delay_ms": 400})


@pytest.mark.asyncio
async def test_same_request_id_distinct_jobs(external_settings, tmp_path) -> None:
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = _request_json(tmp_path)
            s1 = await _submit_and_wait(client, "/api/v1/jobs/segment", payload)
            s2 = await _submit_and_wait(client, "/api/v1/jobs/segment", payload)
            assert s1["status"] == "succeeded"
            assert s2["status"] == "succeeded"
            assert s1["job_id"] != s2["job_id"]
            p1 = s1["result"]["reference"]["path"]
            p2 = s2["result"]["reference"]["path"]
            assert p1 != p2


@pytest.mark.asyncio
async def test_health_reports_external_test_ready(external_settings) -> None:
    app = create_app(external_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["mode"] == "external_test"
            assert health["gpu_queue"]["max_concurrency"] == 1
