from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tests.integration_cpu.conftest import write_tone
from tests.integration_cpu.test_chapter_pipeline import FailSecondQualityAnalyzer, _import_profile
from voice_pipeline.api.app import create_app


class GatedIndexTTSClient:
    """Hold the first IndexTTS inference without blocking chapter creation."""

    def __init__(self) -> None:
        from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient

        self._delegate = FakeIndexTTSClient()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def calls(self) -> int:
        return self._delegate.calls

    def fingerprint(self):
        return self._delegate.fingerprint()

    async def synthesize(self, request, output_path):
        self.started.set()
        await self.release.wait()
        return await self._delegate.synthesize(request, output_path)


@pytest.mark.asyncio
async def test_chapter_routes_submit_status_audio_and_timeline_without_path_leak(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "private-base.wav"
    write_tone(base_voice, 5.0)
    app = create_app(fake_settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            submitted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "chapter",
                    "source_text": "第一句。第二句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice),
                    "model_profile_id": profile_id,
                },
            )
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]
            status = await _wait_status(
                client,
                f"/api/v1/chapters/{run_id}",
                {"succeeded", "failed", "interrupted"},
            )
            payload = status.json()
            progress = await client.get(f"/api/v1/chapters/{run_id}/progress")
            audio = await client.get(f"/api/v1/chapters/{run_id}/audio")
            timeline = await client.get(f"/api/v1/chapters/{run_id}/timeline")
            archive = await client.get(f"/api/v1/chapters/{run_id}/export/gsv")
            active_gsv_ids = [item["active_gsv_version_id"] for item in progress.json()["segments"]]
            first_version = await app.state.plane.version_store.get_version(active_gsv_ids[0])
            first_blob = app.state.plane.artifact_store.root / first_version.blob_relative_path
            first_blob.write_bytes(b"corrupt")
            corrupt_archive = await client.get(f"/api/v1/chapters/{run_id}/export/gsv")
            deleted = await client.delete(f"/api/v1/chapters/{run_id}")
            missing = await client.get(f"/api/v1/chapters/{run_id}")

    assert payload["status"] == "succeeded"
    assert str(base_voice) not in str(payload)
    assert audio.status_code == 200
    assert timeline.status_code == 200
    assert len(timeline.json()["segments"]) == 2
    assert archive.status_code == 200
    assert archive.headers["content-type"].startswith("application/zip")
    assert "gsv-segments.zip" in archive.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert bundle.namelist() == ["001.wav", "002.wav", "manifest.json"]
        assert bundle.read("001.wav")[:4] == b"RIFF"
        assert bundle.read("002.wav")[:4] == b"RIFF"
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == run_id
    assert manifest["title"] == "chapter"
    assert [item["ordinal"] for item in manifest["segments"]] == [0, 1]
    assert [item["version_id"] for item in manifest["segments"]] == active_gsv_ids
    assert str(base_voice) not in json.dumps(manifest, ensure_ascii=False)
    assert str(app.state.plane.artifact_store.root) not in json.dumps(manifest, ensure_ascii=False)
    assert not list((app.state.plane.artifact_store.root / "exports").glob("*.zip"))
    assert corrupt_archive.status_code == 409
    assert corrupt_archive.json()["error"]["code"] == "ARTIFACT_CORRUPT"
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted", "run_id": run_id}
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_chapter_delete_rejects_active_run(fake_settings, tmp_path: Path) -> None:
    from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient

    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "private-base.wav"
    write_tone(base_voice, 5.0)
    app = create_app(fake_settings, index_client=FakeIndexTTSClient(delay_seconds=0.5))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            submitted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "active chapter",
                    "source_text": "这是一句正在生成的测试文本。",
                    "target_language": "zh",
                    "base_voice_path": str(base_voice),
                    "model_profile_id": profile_id,
                },
            )
            run_id = submitted.json()["run_id"]
            deleted = await client.delete(f"/api/v1/chapters/{run_id}")

    assert deleted.status_code == 409
    assert deleted.json()["error"]["code"] == "CHAPTER_STATE_CONFLICT"


@pytest.mark.asyncio
async def test_chapter_submit_returns_before_reference_duration_probe_finishes(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "private-base.wav"
    write_tone(base_voice, 5.0)
    index = GatedIndexTTSClient()
    app = create_app(fake_settings, index_client=index)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            post_task = asyncio.create_task(
                client.post(
                    "/api/v1/chapters",
                    json={
                        "request_id": str(uuid4()),
                        "title": "fast chapter",
                        "source_text": "只有一句。",
                        "target_language": "zh",
                        "base_voice_path": str(base_voice),
                        "model_profile_id": profile_id,
                    },
                )
            )
            completed, _ = await asyncio.wait({post_task}, timeout=5.0)
            returned_before_probe = post_task in completed
            index.release.set()
            submitted = await post_task

            assert returned_before_probe
            assert submitted.status_code == 202
            run_id = submitted.json()["run_id"]
            incomplete_archive = await client.get(f"/api/v1/chapters/{run_id}/export/gsv")
            await asyncio.wait_for(index.started.wait(), timeout=10.0)
            status = await _wait_status(
                client,
                f"/api/v1/chapters/{run_id}",
                {"succeeded", "failed", "interrupted"},
            )

    assert status.json()["status"] == "succeeded"
    assert index.calls == 1
    assert incomplete_archive.status_code == 409
    assert incomplete_archive.json()["error"]["code"] == "CHAPTER_STATE_CONFLICT"
    assert incomplete_archive.json()["error"]["details"]["missing_ordinals"] == [0]


@pytest.mark.asyncio
async def test_chapter_resume_requires_repaired_reference_then_returns_accepted(
    fake_settings, tmp_path: Path
) -> None:
    fake_settings.model_library.models_root = tmp_path / "library"
    fake_settings.model_library.allowed_import_roots = [tmp_path / "models"]
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    original_base_voice = base_voice.read_bytes()
    quality = FailSecondQualityAnalyzer()
    app = create_app(fake_settings)
    app.state.plane.configure_quality(quality)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            profile_id = await _import_profile(client, tmp_path)
            submitted = await client.post(
                "/api/v1/chapters",
                json={
                    "request_id": str(uuid4()),
                    "title": "resume contract",
                    "source_text": "第一句。第二句。",
                    "target_language": "ja",
                    "base_voice_path": str(base_voice),
                    "model_profile_id": profile_id,
                },
            )
            run_id = submitted.json()["run_id"]
            failed = await _wait_status(
                client,
                f"/api/v1/chapters/{run_id}",
                {"succeeded", "failed", "interrupted"},
            )
            assert failed.json()["status"] == "failed"
            progress = (await client.get(f"/api/v1/chapters/{run_id}/progress")).json()
            blocker = progress["segments"][1]
            calls_before_conflict = app.state.plane.index.calls

            write_tone(base_voice, 4.5)
            changed_voice = await client.post(f"/api/v1/chapters/{run_id}/resume")
            assert changed_voice.status_code == 409
            assert changed_voice.json()["error"]["details"] == {
                "action_required": "restore_base_voice"
            }
            base_voice.write_bytes(original_base_voice)

            blocked = await client.post(f"/api/v1/chapters/{run_id}/resume")

            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "CHAPTER_STATE_CONFLICT"
            assert blocked.json()["error"]["details"] == {
                "ordinal": 1,
                "segment_id": blocker["segment_id"],
                "action_required": "regenerate_reference",
                "reference_state": "missing",
                "reference_job_status": "failed",
            }
            assert app.state.plane.index.calls == calls_before_conflict
            repair = await client.post(
                f"/api/v1/segments/{blocker['segment_id']}/regenerate-reference",
                json={"request_id": str(uuid4())},
            )
            assert repair.status_code == 202
            repair_status = await _wait_status(
                client,
                repair.json()["status_url"],
                {"succeeded", "failed"},
            )
            assert repair_status.json()["status"] == "succeeded"

            accepted = await client.post(f"/api/v1/chapters/{run_id}/resume")
            duplicate = await client.post(f"/api/v1/chapters/{run_id}/resume")

            assert accepted.status_code == 202
            assert accepted.json()["run_id"] == run_id
            assert accepted.json()["status"] == "running"
            assert accepted.json()["status_url"] == f"/api/v1/chapters/{run_id}"
            assert duplicate.status_code == 409
            completed = await _wait_status(
                client,
                f"/api/v1/chapters/{run_id}",
                {"succeeded", "failed", "interrupted"},
            )
            assert completed.json()["status"] == "succeeded"


async def _wait_status(
    client: httpx.AsyncClient,
    url: str,
    terminal_statuses: set[str],
    *,
    timeout_seconds: float = 60.0,
) -> httpx.Response:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_status = "unknown"
    while True:
        response = await client.get(url)
        assert response.status_code == 200
        last_status = str(response.json()["status"])
        if last_status in terminal_statuses:
            return response
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(
                f"{url} did not reach {sorted(terminal_statuses)} within "
                f"{timeout_seconds:.0f}s; last status was {last_status}"
            )
        await asyncio.sleep(0.05)
