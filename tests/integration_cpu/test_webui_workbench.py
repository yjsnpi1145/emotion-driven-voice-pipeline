from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.api.app import create_app

WEBUI = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui"


def test_director_preprocessing_review_assets_are_wired() -> None:
    page = (WEBUI / "index.html").read_text(encoding="utf-8")
    script = (WEBUI / "director.js").read_text(encoding="utf-8")
    stylesheet = (WEBUI / "styles.css").read_text(encoding="utf-8")

    assert 'name="preprocessing_mode"' in page
    assert 'id="director-preprocess-review"' in page
    assert 'id="director-preprocess-list"' in page
    assert 'id="director-confirm-preprocessing"' in page
    assert "创建并开始预处理" in page
    assert 'from "./director-preprocessing.js?v=20260828a"' in script
    assert "/confirm-preprocessing" in script
    assert "/preprocess-paragraphs/" in script
    assert "IntersectionObserver" in script
    assert ".director-preprocess-grid" in stylesheet
    assert "director-preprocessing.js" in {
        item.name for item in WEBUI.iterdir() if item.is_file()
    }


async def _import_profile(client: httpx.AsyncClient, tmp_path: Path) -> str:
    source = tmp_path / "models"
    source.mkdir()
    gpt = source / "voice.ckpt"
    sovits = source / "voice.pth"
    gpt.write_bytes(b"gpt")
    sovits.write_bytes(b"sovits")
    created = await client.post(
        "/api/v1/model-profiles/import",
        json={
            "display_name": "workbench-voice",
            "gpt_source_path": str(gpt.resolve()),
            "sovits_source_path": str(sovits.resolve()),
        },
    )
    assert created.status_code == 201
    profile_id = created.json()["profile_id"]
    assert (await client.post(f"/api/v1/model-profiles/{profile_id}/activate")).status_code == 200
    return profile_id


async def _wait_for_chapter(client: httpx.AsyncClient, run_id: str) -> dict[str, object]:
    for _ in range(400):
        response = await client.get(f"/api/v1/chapters/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"succeeded", "failed", "interrupted"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("chapter did not complete")


@pytest.mark.asyncio
async def test_workbench_chapter_progress_draft_edit_and_current_audio(
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
            profile_id = await _import_profile(client, tmp_path)
            accepted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "webui chapter",
                    "source_text": "第一句。第二句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice.resolve()),
                    "model_profile_id": profile_id,
                },
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["run_id"]
            assert (await _wait_for_chapter(client, run_id))["status"] == "succeeded"

            progress = await client.get(f"/api/v1/chapters/{run_id}/progress")
            assert progress.status_code == 200
            public_run = (await client.get(f"/api/v1/chapters/{run_id}")).json()
            assert public_run["title"] == "webui chapter"
            assert public_run["final_audio_url"] == f"/api/v1/chapters/{run_id}/audio"
            assert str(base_voice) not in str(public_run)
            rows = progress.json()["segments"]
            assert len(rows) == 2
            assert all(row["active_gsv_version_id"] for row in rows)
            assert "base_voice_path" not in progress.text

            segment_id = rows[0]["segment_id"]
            before = await client.get(f"/api/v1/segments/{segment_id}")
            assert before.status_code == 200
            before_payload = before.json()
            jobs_before = await app.state.plane.database.scalar_int(
                "SELECT count(*) FROM generation_jobs"
            )
            patched = await client.patch(
                f"/api/v1/segments/{segment_id}/inputs",
                json={
                    "expected_ref_draft_revision": before_payload["ref_draft_revision"],
                    "expected_gsv_draft_revision": before_payload["gsv_draft_revision"],
                    "ref_text_cn": "这是手动微调后的中文参考文本。",
                    "synthesis_text": "これは手動で編集した日本語です。",
                    "current_emotion_vector": [0.1] * 8,
                },
            )
            assert patched.status_code == 200
            assert patched.json()["ref_draft_revision"] == before_payload["ref_draft_revision"] + 1
            assert patched.json()["gsv_draft_revision"] == before_payload["gsv_draft_revision"] + 1
            assert (
                await app.state.plane.database.scalar_int("SELECT count(*) FROM generation_jobs")
                == jobs_before
            )
            derived_progress = (await client.get(f"/api/v1/chapters/{run_id}/progress")).json()[
                "segments"
            ]
            assert derived_progress[0]["reference_state"] == "draft_pending"
            assert derived_progress[0]["gsv_state"] == "stale"

            ui_script = (
                Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui" / "app.js"
            ).read_text(encoding="utf-8")
            assert "progress.reference_state" in ui_script
            assert "progress.gsv_state" in ui_script
            assert "runSelectionGeneration" in ui_script
            assert "editorDraftDirty" in ui_script
            assert "saveInFlight" in ui_script
            assert "setEditorSaving(true)" in ui_script
            assert "setEditorSaving(false)" in ui_script

            audio = await client.get(f"/api/v1/versions/{rows[0]['active_gsv_version_id']}/audio")
            assert audio.status_code == 200
            assert audio.headers["content-type"].startswith("audio/wav")
            assert audio.content[:4] == b"RIFF"
