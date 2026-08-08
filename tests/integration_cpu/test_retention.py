from __future__ import annotations

import httpx
import pytest
from sqlalchemy import update

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_segment_bound_jobs import _create_segment, _wait
from voice_pipeline.api.app import create_app
from voice_pipeline.storage.orm import artifact_version_state


@pytest.mark.asyncio
async def test_retention_keeps_current_and_latest_five_non_current_versions(
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
            for index in range(7):
                submission = await client.post(
                    f"/api/v1/segments/{segment_id}/jobs/reference",
                    json={
                        "request_id": f"00000000-0000-4000-8000-{index + 1:012d}",
                        "base_voice_path": str(base_voice.resolve()),
                        "activate_on_success": False,
                    },
                )
                assert submission.status_code == 202
                assert (await _wait(client, submission.json()["job_id"]))["status"] == "succeeded"
            versions = (await client.get(f"/api/v1/segments/{segment_id}/versions")).json()
            oldest = versions[-1]
            activated = await client.post(
                f"/api/v1/segments/{segment_id}/versions/{oldest['version_id']}/activate",
                json={"expected_selection_revision": 0},
            )
            assert activated.status_code == 200
            plan = await client.post("/api/v1/maintenance/retention/plan")
            replacement = await client.post(
                f"/api/v1/segments/{segment_id}/versions/{versions[0]['version_id']}/activate",
                json={"expected_selection_revision": 1},
            )
            assert replacement.status_code == 200
            stale_apply = await client.post(
                f"/api/v1/maintenance/retention/{plan.json()['plan_id']}/apply"
            )
            fresh_plan = await client.post("/api/v1/maintenance/retention/plan")
            applied = await client.post(
                f"/api/v1/maintenance/retention/{fresh_plan.json()['plan_id']}/apply"
            )
            applied_again = await client.post(
                f"/api/v1/maintenance/retention/{fresh_plan.json()['plan_id']}/apply"
            )
            ready_versions = await client.get(f"/api/v1/segments/{segment_id}/versions")
            deleted_version_id = applied.json()["deleted_version_ids"][0]
            deleted_version = (
                await client.get(f"/api/v1/versions/{deleted_version_id}")
            ).json()

    assert plan.status_code == 201
    payload = plan.json()
    assert payload["candidate_version_ids"] == [versions[-2]["version_id"]]
    assert stale_apply.status_code == 409
    assert stale_apply.json()["error"]["code"] == "RETENTION_PLAN_STALE"
    assert applied.status_code == 200
    assert applied_again.json() == applied.json()
    assert len(ready_versions.json()) == 6
    manifest = app.state.plane.artifact_store.root / deleted_version["manifest_relative_path"]
    trash_manifest = (
        app.state.plane.artifact_store.root / "trash" / "retention" / "manifests" / manifest.name
    )
    assert deleted_version["state"] == "deleted"
    assert not manifest.exists()
    assert trash_manifest.is_file()


@pytest.mark.asyncio
async def test_startup_finishes_a_crash_left_retention_deletion(fake_settings, tmp_path) -> None:
    """A durable deleting marker never remains indefinitely after a restart."""
    base_voice = tmp_path / "voice.wav"
    write_tone(base_voice, seconds=5.0)
    first = create_app(fake_settings)
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client:
            segment_id = await _create_segment(client)
            initial = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "8a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                },
            )
            assert (await _wait(client, initial.json()["job_id"]))["status"] == "succeeded"
            patched = await client.patch(
                f"/api/v1/segments/{segment_id}/inputs",
                json={
                    "expected_ref_draft_revision": 0,
                    "expected_gsv_draft_revision": 0,
                    "seed": 2,
                },
            )
            assert patched.status_code == 200
            second = await client.post(
                f"/api/v1/segments/{segment_id}/jobs/reference",
                json={
                    "request_id": "9a7f8b25-c247-4495-8f34-09f741dbed7a",
                    "base_voice_path": str(base_voice.resolve()),
                    "activate_on_success": False,
                },
            )
            assert (await _wait(client, second.json()["job_id"]))["status"] == "succeeded"
            versions = (await client.get(f"/api/v1/segments/{segment_id}/versions")).json()
            second_job_id = second.json()["job_id"]
            candidate_id = next(
                item["version_id"] for item in versions if item["source_job_id"] == second_job_id
            )
            async with first.state.plane.database.write_session() as session:
                await session.execute(
                    update(artifact_version_state)
                    .where(artifact_version_state.c.version_id == candidate_id)
                    .values(state="deleting")
                )

    second_app = create_app(fake_settings)
    async with second_app.router.lifespan_context(second_app):
        recovered = await second_app.state.plane.version_store.get_version(candidate_id)

    assert recovered.state == "deleted"
