from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.contract.conftest import EXPECTED_INDEX_FINGERPRINT, write_valid_reference
from workers.indextts2.app import create_worker_app
from workers.indextts2.engine import FakeWorkerEngine
from workers.indextts2.schemas import WorkerSynthesisRequest


def _payload(tmp_path: Path, *, output_name: str = "reference.wav") -> dict[str, object]:
    return {
        "request_id": "d613a571-1d69-4f6e-a1b7-3222f61657b8",
        "text": "我已经失去了一切，可我仍然活着。",
        "speaker_audio_path": str((tmp_path / "voice.wav").resolve()),
        "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        "seed": 1234,
        "use_random": False,
        "output_path": str((tmp_path / "jobs" / output_name).resolve()),
    }


@pytest.mark.asyncio
async def test_worker_health_and_synthesize(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    write_valid_reference(tmp_path / "voice.wav")
    app = create_worker_app(
        FakeWorkerEngine(),
        jobs_root=jobs_root,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        live = await client.get("/health/live")
        assert live.status_code == 200

        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        ready_body = ready.json()
        assert ready_body["state"] == "ready"
        assert ready_body["fingerprint"] == EXPECTED_INDEX_FINGERPRINT.model_dump(mode="json")

        payload = _payload(tmp_path)
        resp = await client.post("/v1/synthesize", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["request_id"] == payload["request_id"]
        assert body["effective_emotion_vector"] == list(payload["emotion_vector"])
        assert (await asyncio.to_thread(Path(body["output_path"]).is_file)) is True
        assert body["engine_fingerprint"] == EXPECTED_INDEX_FINGERPRINT.model_dump(mode="json")


@pytest.mark.asyncio
async def test_worker_rejects_output_outside_jobs_root(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    app = create_worker_app(
        FakeWorkerEngine(),
        jobs_root=jobs_root,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
    )
    payload = _payload(tmp_path)
    outside = tmp_path / "outside" / "x.wav"
    payload["output_path"] = str(outside.resolve())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post("/v1/synthesize", json=payload)
        assert resp.status_code == 403
        assert not outside.exists()


@pytest.mark.asyncio
async def test_worker_rejects_existing_output(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    existing = jobs_root / "reference.wav"
    existing.write_bytes(b"sentinel")
    app = create_worker_app(
        FakeWorkerEngine(),
        jobs_root=jobs_root,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
    )
    payload = _payload(tmp_path)
    payload["output_path"] = str(existing.resolve())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post("/v1/synthesize", json=payload)
        assert resp.status_code == 409
        assert existing.read_bytes() == b"sentinel"


@pytest.mark.asyncio
async def test_worker_rejects_non_wav_output(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    app = create_worker_app(
        FakeWorkerEngine(),
        jobs_root=jobs_root,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
    )
    payload = _payload(tmp_path, output_name="reference.txt")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post("/v1/synthesize", json=payload)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_worker_engine_failure_returns_500(tmp_path: Path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    app = create_worker_app(
        FakeWorkerEngine(
            failure=PipelineError(ErrorCode.INDEX_ENGINE_ERROR, "index", "boom", retryable=False)
        ),
        jobs_root=jobs_root,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
    )
    payload = _payload(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        resp = await client.post("/v1/synthesize", json=payload)
        assert resp.status_code == 500
        assert (await asyncio.to_thread(Path(payload["output_path"]).exists)) is False


def test_worker_schema_rejects_blank_text(tmp_path: Path) -> None:
    from pydantic import ValidationError

    payload = _payload(tmp_path)
    payload["text"] = "   "
    with pytest.raises(ValidationError):
        WorkerSynthesisRequest.model_validate(payload)


def test_worker_schema_dump_roundtrip(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    req = WorkerSynthesisRequest.model_validate(payload)
    dumped = json.loads(req.model_dump_json())
    assert dumped["emotion_vector"] == list(payload["emotion_vector"])
