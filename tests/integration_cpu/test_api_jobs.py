from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app
from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient


@pytest.mark.asyncio
async def test_segment_job_completes_through_single_queue(fake_settings, request_json) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            assert submitted.status_code == 202
            job_id = submitted.json()["job_id"]

            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)

            assert status["status"] == "succeeded"
            assert (await client.get(f"/api/v1/jobs/{job_id}/audio/reference")).status_code == 200
            assert (await client.get(f"/api/v1/jobs/{job_id}/audio/target")).status_code == 200
            assert (
                await client.get(f"/api/v1/jobs/{job_id}/manifest/reference")
            ).status_code == 200
            assert (await client.get(f"/api/v1/jobs/{job_id}/manifest/run")).status_code == 200
            health = (await client.get("/api/v1/health")).json()
            assert health["gpu_queue"]["max_active_observed"] == 1


@pytest.mark.asyncio
async def test_same_request_id_gets_distinct_jobs_and_dirs(fake_settings, request_json) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/jobs/segment", json=request_json)
            second = await client.post("/api/v1/jobs/segment", json=request_json)
            assert first.status_code == 202
            assert second.status_code == 202
            first_id = first.json()["job_id"]
            second_id = second.json()["job_id"]
            assert first_id != second_id

            for _ in range(200):
                s1 = (await client.get(f"/api/v1/jobs/{first_id}")).json()
                s2 = (await client.get(f"/api/v1/jobs/{second_id}")).json()
                if s1["status"] in {"succeeded", "failed"} and s2["status"] in {
                    "succeeded",
                    "failed",
                }:
                    break
                await asyncio.sleep(0.01)

            assert s1["status"] == "succeeded"
            assert s2["status"] == "succeeded"
            p1 = s1["result"]["reference"]["path"]
            p2 = s2["result"]["reference"]["path"]
            assert p1 != p2


@pytest.mark.asyncio
async def test_waiting_job_times_out_and_marks_failed(
    fake_settings, request_json, tmp_path
) -> None:
    slow_index = FakeIndexTTSClient(delay_seconds=3.0)
    app = create_app(fake_settings, index_client=slow_index)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/jobs/segment", json=request_json)
            first_id = first.json()["job_id"]
            second = await client.post("/api/v1/jobs/segment", json=request_json)
            second_id = second.json()["job_id"]

            for _ in range(200):
                s1 = (await client.get(f"/api/v1/jobs/{first_id}")).json()
                s2 = (await client.get(f"/api/v1/jobs/{second_id}")).json()
                if s1["status"] in {"succeeded", "failed"} and s2["status"] in {
                    "succeeded",
                    "failed",
                }:
                    break
                await asyncio.sleep(0.05)

            assert s2["status"] == "failed"
            assert s2["error"]["code"] == "QUEUE_TIMEOUT"
            assert s1["status"] == "succeeded"


@pytest.mark.asyncio
async def test_gsv_job_uses_portable_reference_manifest(
    fake_settings, request_json, tmp_path
) -> None:
    import json as _json

    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ref_payload = {
                key: value
                for key, value in request_json.items()
                if key not in ("target_text", "target_language")
            }
            ref = await client.post("/api/v1/jobs/reference", json=ref_payload)
            ref_id = ref.json()["job_id"]
            for _ in range(100):
                ref_status = (await client.get(f"/api/v1/jobs/{ref_id}")).json()
                if ref_status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert ref_status["status"] == "succeeded"

            manifest = (await client.get(f"/api/v1/jobs/{ref_id}/manifest/reference")).json()

            # Write a portable manifest on disk and point the gsv job at it.
            portable = tmp_path / "portable" / "reference.reference-manifest.json"
            portable.parent.mkdir(parents=True)
            portable.write_text(_json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            gsv_payload = {
                "request_id": "4de7ed6a-00f0-4be6-b916-1f10cf96019e",
                "reference_manifest_path": str(portable.resolve()),
                "target_text": "私はまだ生きている。",
                "target_language": "ja",
                "speed_factor": 1.0,
                "seed": 1234,
            }

            gsv = await client.post("/api/v1/jobs/gsv", json=gsv_payload)
            assert gsv.status_code == 202
            gsv_id = gsv.json()["job_id"]
            for _ in range(100):
                gsv_status = (await client.get(f"/api/v1/jobs/{gsv_id}")).json()
                if gsv_status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert gsv_status["status"] == "succeeded"
            assert (await client.get(f"/api/v1/jobs/{gsv_id}/audio/target")).status_code == 200
            assert (
                gsv_status["result"]["reference_content_sha256"]
                == manifest["reference"]["audio"]["content_sha256"]
            )
