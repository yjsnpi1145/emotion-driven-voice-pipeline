from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_segment_regeneration import _profile, _wait
from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_public_version_history_is_path_free_and_explicit_compose_uses_current_selection(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _profile(client, tmp_path)
            accepted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "history",
                    "source_text": "第一句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice.resolve()),
                    "model_profile_id": profile_id,
                },
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["run_id"]
            assert (await _wait(client, f"/api/v1/chapters/{run_id}"))["status"] == "succeeded"
            row = (await client.get(f"/api/v1/chapters/{run_id}/progress")).json()["segments"][0]

            history = await client.get(f"/api/v1/segments/{row['segment_id']}/history")
            assert history.status_code == 200
            assert str(base_voice) not in history.text
            body = history.json()
            assert body["reference"][0]["audio_url"].endswith("/audio")
            assert body["gsv"][0]["ref_version_id"] == row["active_ref_version_id"]

            regenerated = await client.post(
                f"/api/v1/segments/{row['segment_id']}/regenerate-reference",
                json={"request_id": str(uuid4()), "base_voice_path": str(base_voice.resolve())},
            )
            assert regenerated.status_code == 202
            assert (await _wait(client, regenerated.json()["status_url"]))["status"] == "succeeded"
            stale_history = await client.get(f"/api/v1/segments/{row['segment_id']}/history")
            assert stale_history.status_code == 200
            stale_body = stale_history.json()
            assert stale_body["state"]["gsv"] == "stale"
            previous_gsv = stale_body["gsv"][-1]
            activated = await client.post(
                f"/api/v1/segments/{row['segment_id']}/versions/{previous_gsv['version_id']}/activate",
                json={"expected_selection_revision": stale_body["selection_revision"]},
            )
            assert activated.status_code == 200
            assert activated.json()["active_gsv_version_id"] == previous_gsv["version_id"]
            segment = await client.get(f"/api/v1/segments/{row['segment_id']}")
            assert segment.status_code == 200
            restored = await client.post(
                f"/api/v1/segments/{row['segment_id']}/versions/{previous_gsv['version_id']}/restore-inputs",
                json={
                    "expected_ref_draft_revision": segment.json()["ref_draft_revision"],
                    "expected_gsv_draft_revision": segment.json()["gsv_draft_revision"],
                },
            )
            assert restored.status_code == 200

            composed = await client.post(f"/api/v1/chapters/{run_id}/compose")
            assert composed.status_code == 200
            assert composed.json()["status"] == "succeeded"
