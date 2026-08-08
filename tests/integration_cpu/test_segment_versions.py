from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from tests.unit.conftest import write_tone
from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_task_segment_crud_preserves_llm_vector_and_rejects_bad_source_slice(
    fake_settings,
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            task_response = await client.post(
                "/api/v1/tasks",
                json={
                    "title": "第一章",
                    "source_text": "前半句。后半句。",
                    "target_language": "ja",
                    "output_spec": {"format": "wav", "sample_rate": 32000},
                },
            )
            assert task_response.status_code == 201
            task_id = task_response.json()["task_id"]

            mismatch = await client.post(
                f"/api/v1/tasks/{task_id}/segments",
                json={
                    "ordinal": 0,
                    "source_start": 0,
                    "source_end": 4,
                    "source_text": "被改写的文本",
                    "synthesis_text": "前半句。",
                    "llm_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.25, 0.0, 0.15],
                    "ref_text_cn": "我仍然活着。",
                    "speed_factor": 0.95,
                    "pause_after_ms": 500,
                    "seed": 1234,
                },
            )
            assert mismatch.status_code == 422
            assert mismatch.json()["error"]["code"] == "INVALID_INPUT"

            created = await client.post(
                f"/api/v1/tasks/{task_id}/segments",
                json={
                    "ordinal": 0,
                    "source_start": 0,
                    "source_end": 4,
                    "source_text": "前半句。",
                    "synthesis_text": "前半句。",
                    "llm_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.25, 0.0, 0.15],
                    "ref_text_cn": "我仍然活着。",
                    "speed_factor": 0.95,
                    "pause_after_ms": 500,
                    "seed": 1234,
                },
            )
            assert created.status_code == 201
            segment = created.json()
            assert segment["current_emotion_vector"] == segment["llm_emotion_vector"]
            segment_id = segment["segment_id"]

            patched = await client.patch(
                f"/api/v1/segments/{segment_id}/inputs",
                json={
                    "expected_ref_draft_revision": 0,
                    "expected_gsv_draft_revision": 0,
                    "current_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.2, 0.0, 0.2],
                    "synthesis_text": "変更済み。",
                },
            )
            assert patched.status_code == 200
            assert patched.json()["ref_draft_revision"] == 1
            assert patched.json()["gsv_draft_revision"] == 1

            stale = await client.patch(
                f"/api/v1/segments/{segment_id}/inputs",
                json={
                    "expected_ref_draft_revision": 0,
                    "expected_gsv_draft_revision": 0,
                    "pause_after_ms": 300,
                },
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_immutable_version_can_be_listed_activated_and_downloaded(
    fake_settings, tmp_path
) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            task = await client.post(
                "/api/v1/tasks",
                json={
                    "title": "版本任务",
                    "source_text": "测试。",
                    "target_language": "ja",
                    "output_spec": {"format": "wav", "sample_rate": 32000},
                },
            )
            task_id = task.json()["task_id"]
            segment = await client.post(
                f"/api/v1/tasks/{task_id}/segments",
                json={
                    "ordinal": 0,
                    "source_start": 0,
                    "source_end": 3,
                    "source_text": "测试。",
                    "synthesis_text": "test",
                    "llm_emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.25, 0.0, 0.15],
                    "ref_text_cn": "测试。",
                    "speed_factor": 1.0,
                    "pause_after_ms": 0,
                    "seed": 1,
                },
            )
            segment_id = segment.json()["segment_id"]
            source = tmp_path / "reference.wav"
            write_tone(source, seconds=4.0)
            job = await app.state.plane.registry.create(
                request_id=uuid4(), kind="reference", request_snapshot={}
            )
            assert await app.state.plane.registry.mark_running(job.job_id)
            assert await app.state.plane.registry.mark_succeeded(job.job_id, result={"ok": True})
            blob = app.state.plane.artifact_store.publish_blob(
                app.state.plane.artifact_store.stage_audio(job.job_id, source)
            )
            version = await app.state.plane.version_store.create_version(
                segment_id=UUID(segment_id),
                artifact_type="reference",
                source_job_id=job.job_id,
                blob=blob,
                input_snapshot={},
                model_fingerprint={},
                quality_result={},
            )

            listed = await client.get(f"/api/v1/segments/{segment_id}/versions")
            activated = await client.post(
                f"/api/v1/segments/{segment_id}/versions/{version.version_id}/activate",
                json={"expected_selection_revision": 0},
            )
            audio = await client.get(f"/api/v1/versions/{version.version_id}/audio")

    assert listed.status_code == 200
    assert [item["version_id"] for item in listed.json()] == [str(version.version_id)]
    assert activated.status_code == 200
    assert activated.json()["active_ref_version_id"] == str(version.version_id)
    assert audio.status_code == 200
