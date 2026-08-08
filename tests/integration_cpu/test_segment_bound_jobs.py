from __future__ import annotations

import asyncio
import json

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


@pytest.mark.asyncio
async def test_segment_gsv_job_freezes_the_active_model_profile_at_submission(
    fake_settings, tmp_path
) -> None:
    """Changing the UI-selected profile later cannot alter an already queued segment job."""
    source = tmp_path / "source-models"
    source.mkdir()
    gpt = source / "voice.ckpt"
    sovits = source / "voice.pth"
    gpt_next = source / "voice-next.ckpt"
    sovits_next = source / "voice-next.pth"
    gpt.write_bytes(b"gpt-v1")
    sovits.write_bytes(b"sovits-v1")
    gpt_next.write_bytes(b"gpt-v2")
    sovits_next.write_bytes(b"sovits-v2")
    fake_settings.model_library.models_root = tmp_path / "model-library"
    fake_settings.model_library.allowed_import_roots = [source]
    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            imported = await client.post(
                "/api/v1/model-profiles/import",
                json={
                    "display_name": "frozen-voice",
                    "gpt_source_path": str(gpt.resolve()),
                    "sovits_source_path": str(sovits.resolve()),
                },
            )
            profile_id = imported.json()["profile_id"]
            imported_next = await client.post(
                "/api/v1/model-profiles/import",
                json={
                    "display_name": "later-voice",
                    "gpt_source_path": str(gpt_next.resolve()),
                    "sovits_source_path": str(sovits_next.resolve()),
                },
            )
            next_profile_id = imported_next.json()["profile_id"]
            activated = await client.post(f"/api/v1/model-profiles/{profile_id}/activate")
            assert activated.status_code == 200
            segment_id = await _create_segment(client)
            ref = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "4a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                },
            )
            assert (await _wait(client, ref.json()["job_id"]))["status"] == "succeeded"
            submitted = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/gsv",
                json={"request_id": "5a7f8b25-c247-4495-8f34-09f741dbed7a"},
            )
            assert submitted.status_code == 202
            queued = await client.get(f"/api/v1/jobs/{submitted.json()['job_id']}")
            switched = await client.post(f"/api/v1/model-profiles/{next_profile_id}/activate")
            assert switched.status_code == 200
            completed = await _wait(client, submitted.json()["job_id"])
            versions = await client.get(f"/api/v1/segments/{segment_id}/versions")

    assert queued.json()["request_snapshot"]["model_profile_id"] == profile_id
    assert queued.json()["model_profile_snapshot"]["profile"]["profile_id"] == profile_id
    assert completed["status"] == "succeeded"
    gsv_version = next(item for item in versions.json() if item["artifact_type"] == "gsv")
    assert gsv_version["model_profile_snapshot"]["profile"]["profile_id"] == profile_id
    assert [str(item.profile_id) for item in app.state.plane.gsv.loaded_profiles] == [profile_id]
    manifest_path = app.state.plane.artifact_store.root / gsv_version["manifest_relative_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version_id"] == gsv_version["version_id"]
    assert manifest["model_profile_snapshot"]["profile"]["profile_id"] == profile_id


@pytest.mark.asyncio
async def test_direct_gsv_job_freezes_active_model_profile_before_queue_execution(
    fake_settings, tmp_path
) -> None:
    """A direct /jobs/gsv submission must not follow a later active-profile switch."""
    from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient

    source = tmp_path / "source-models"
    source.mkdir()
    gpt_a, sovits_a = source / "a.ckpt", source / "a.pth"
    gpt_b, sovits_b = source / "b.ckpt", source / "b.pth"
    gpt_a.write_bytes(b"gpt-a")
    sovits_a.write_bytes(b"sovits-a")
    gpt_b.write_bytes(b"gpt-b")
    sovits_b.write_bytes(b"sovits-b")
    fake_settings.model_library.models_root = tmp_path / "model-library"
    fake_settings.model_library.allowed_import_roots = [source]

    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    app = create_app(fake_settings, index_client=FakeIndexTTSClient(delay_seconds=0.25))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            imported_a = await client.post(
                "/api/v1/model-profiles/import",
                json={
                    "display_name": "voice-a",
                    "gpt_source_path": str(gpt_a.resolve()),
                    "sovits_source_path": str(sovits_a.resolve()),
                },
            )
            imported_b = await client.post(
                "/api/v1/model-profiles/import",
                json={
                    "display_name": "voice-b",
                    "gpt_source_path": str(gpt_b.resolve()),
                    "sovits_source_path": str(sovits_b.resolve()),
                },
            )
            profile_a = imported_a.json()["profile_id"]
            profile_b = imported_b.json()["profile_id"]
            activated_a = await client.post(f"/api/v1/model-profiles/{profile_a}/activate")
            assert activated_a.status_code == 200

            reference = await client.post(
                "/api/v1/jobs/reference",
                json={
                    "request_id": "6a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                    "ref_text_cn": "这是供直接 GSV 任务使用的参考文本。",
                    "emotion_vector": [0.0, 0.0, 0.2, 0.0, 0.0, 0.25, 0.0, 0.15],
                },
            )
            reference_record = await _wait(client, reference.json()["job_id"])
            assert reference_record["status"] == "succeeded"
            manifest_path = reference_record["result"]["manifest_path"]

            blocker = await client.post(
                "/api/v1/jobs/reference",
                json={
                    "request_id": "7a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                    "ref_text_cn": "这条任务故意占用队列，让 GSV 保持排队。",
                    "emotion_vector": [0.0, 0.0, 0.21, 0.0, 0.0, 0.24, 0.0, 0.15],
                },
            )
            blocker_id = blocker.json()["job_id"]
            for _ in range(100):
                blocker_status = (await client.get(f"/api/v1/jobs/{blocker_id}")).json()
                if blocker_status["status"] == "running":
                    break
                await asyncio.sleep(0.01)

            submitted = await client.post(
                "/api/v1/jobs/gsv",
                json={
                    "request_id": "8a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "reference_manifest_path": manifest_path,
                    "target_text": "This direct job must retain voice A.",
                    "target_language": "en",
                },
            )
            assert submitted.status_code == 202
            queued = await client.get(f"/api/v1/jobs/{submitted.json()['job_id']}")
            activated_b = await client.post(f"/api/v1/model-profiles/{profile_b}/activate")
            assert activated_b.status_code == 200
            completed = await _wait(client, submitted.json()["job_id"])

    assert queued.json()["request_snapshot"]["model_profile_id"] == profile_a
    assert queued.json()["model_profile_snapshot"]["profile"]["profile_id"] == profile_a
    assert completed["status"] == "succeeded"
    assert [str(item.profile_id) for item in app.state.plane.gsv.loaded_profiles] == [profile_a]
