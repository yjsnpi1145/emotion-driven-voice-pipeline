from __future__ import annotations

import asyncio

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.api.app import create_app


async def _wait(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(200):
        record = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if record["status"] in {"succeeded", "failed", "cancelled"}:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def _create_segment(client: httpx.AsyncClient) -> str:
    task = await client.post(
        "/api/v1/tasks",
        json={
            "title": "Segment task",
            "source_text": "test.",
            "target_language": "ja",
            "output_spec": {"format": "wav", "sample_rate": 32000},
        },
    )
    assert task.status_code == 201
    segment = await client.post(
        f"/api/v1/tasks/{task.json()['task_id']}/segments",
        json={
            "ordinal": 0,
            "source_start": 0,
            "source_end": 5,
            "source_text": "test.",
            "synthesis_text": "test",
            "llm_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.25, 0.0, 0.15],
            "ref_text_cn": "reference",
            "speed_factor": 1.0,
            "pause_after_ms": 0,
            "seed": 1,
        },
    )
    assert segment.status_code == 201
    return segment.json()["segment_id"]


@pytest.mark.asyncio
async def test_segment_reference_and_gsv_jobs_commit_immutable_versions(
    fake_settings, tmp_path
) -> None:
    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            segment_id = await _create_segment(client)
            reference_submission = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "0a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                    "activate_on_success": True,
                },
            )
            assert reference_submission.status_code == 202
            reference_job = await _wait(client, reference_submission.json()["job_id"])
            assert reference_job["status"] == "succeeded"
            assert reference_job["activation_outcome"] == "activated"

            after_reference = await client.get(f"/api/v1/segments/{segment_id}")
            reference_version_id = after_reference.json()["active_ref_version_id"]
            assert reference_version_id
            assert (
                await client.get(f"/api/v1/versions/{reference_version_id}/audio")
            ).status_code == 200

            gsv_submission = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/gsv",
                json={
                    "request_id": "2a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "activate_on_success": True,
                },
            )
            assert gsv_submission.status_code == 202
            gsv_job = await _wait(client, gsv_submission.json()["job_id"])
            assert gsv_job["status"] == "succeeded"
            assert gsv_job["activation_outcome"] == "activated"

            versions = await client.get(f"/api/v1/segments/{segment_id}/versions")
            gsv_versions = [item for item in versions.json() if item["artifact_type"] == "gsv"]
            assert len(gsv_versions) == 1
            assert gsv_versions[0]["ref_version_id"] == reference_version_id
            assert gsv_versions[0]["ref_content_sha256"]


@pytest.mark.asyncio
async def test_late_segment_reference_is_preserved_as_history_without_replacing_current(
    fake_settings, tmp_path
) -> None:
    from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient

    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    app = create_app(fake_settings, index_client=FakeIndexTTSClient(delay_seconds=0.25))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            segment_id = await _create_segment(client)
            submitted = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "3a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                    "activate_on_success": True,
                },
            )
            assert submitted.status_code == 202
            await asyncio.sleep(0.05)
            patched = await client.patch(
                f"/api/v1/segments/{segment_id}/inputs",
                json={
                    "expected_ref_draft_revision": 0,
                    "expected_gsv_draft_revision": 0,
                    "current_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.2, 0.0, 0.2],
                },
            )
            assert patched.status_code == 200
            completed = await _wait(client, submitted.json()["job_id"])
            segment = await client.get(f"/api/v1/segments/{segment_id}")
            versions = await client.get(f"/api/v1/segments/{segment_id}/versions")

    assert completed["status"] == "succeeded"
    assert completed["activation_outcome"] == "history_only"
    assert segment.json()["active_ref_version_id"] is None
    assert len(versions.json()) == 1
