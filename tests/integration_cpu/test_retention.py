from __future__ import annotations

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_segment_bound_jobs import _create_segment, _wait
from voice_pipeline.api.app import create_app


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

    assert plan.status_code == 201
    payload = plan.json()
    assert payload["candidate_version_ids"] == [versions[-2]["version_id"]]
    assert stale_apply.status_code == 409
    assert stale_apply.json()["error"]["code"] == "RETENTION_PLAN_STALE"
    assert applied.status_code == 200
    assert applied_again.json() == applied.json()
    assert len(ready_versions.json()) == 6
