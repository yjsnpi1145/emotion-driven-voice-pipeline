from __future__ import annotations

import httpx
import pytest

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
