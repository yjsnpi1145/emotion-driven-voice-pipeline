from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.config import load_settings
from voice_pipeline.models.schemas import EngineFingerprint


def _fingerprint(engine: str, salt: str) -> EngineFingerprint:
    import hashlib

    def h(field: str) -> str:
        return hashlib.sha256(f"{engine}:{salt}:{field}".encode()).hexdigest()

    return EngineFingerprint(
        schema_version=1,
        engine=engine,  # type: ignore[arg-type]
        source_revision=f"{engine}-src-{salt}",
        model_revision="1",
        engine_lock_sha256=h("engine-lock"),
        checkpoint_lock_sha256=h("checkpoint-lock"),
        environment_lock_sha256=h("environment-lock"),
        runtime_config_sha256=h("runtime-config"),
    )


EXPECTED_INDEX_FINGERPRINT = _fingerprint("indextts", "contract-index")
EXPECTED_GSV_FINGERPRINT = _fingerprint("gpt_sovits", "contract-gsv")


def write_valid_reference(path: Path, seconds: float = 4.0) -> None:
    sample_rate = 22050
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = 0.2 * np.sin(2 * np.pi * 220 * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def valid_wav_bytes(seconds: float = 1.5, sample_rate: int = 32000) -> bytes:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = 0.2 * np.sin(2 * np.pi * 300 * t)
    buffer = io.BytesIO()
    sf.write(buffer, data.astype(np.float32), sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


@pytest.fixture
def index_request(tmp_path: Path):
    from voice_pipeline.models.schemas import IndexSynthesisRequest

    return IndexSynthesisRequest(
        request_id="d613a571-1d69-4f6e-a1b7-3222f61657b8",
        text="我已经失去了一切，可我仍然活着。",
        speaker_audio_path=(tmp_path / "voice.wav").resolve(),
        emotion_vector=[0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        seed=1234,
    )


@pytest.fixture
def gsv_request(tmp_path: Path):
    from voice_pipeline.models.schemas import GsvSynthesisRequest, ReferenceBinding

    return GsvSynthesisRequest(
        request_id="d613a571-1d69-4f6e-a1b7-3222f61657b8",
        reference=ReferenceBinding(
            audio={
                "path": str((tmp_path / "reference.wav").resolve()),
                "duration_seconds": 4.0,
                "sample_rate": 22050,
                "channels": 1,
                "frames": 88200,
                "content_sha256": "0" * 64,
                "rms_dbfs": -17.0,
                "peak_dbfs": -14.0,
            },
            ref_text_cn="我已经失去了一切，可我仍然活着。",
            emotion_vector=[0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
            base_voice_sha256="1" * 64,
            engine_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        ),
        text="私はまだ生きている。",
        text_lang="ja",
        seed=1234,
    )


@pytest.fixture
def doctor_payload() -> dict[str, object]:
    """A strict, valid doctor payload matching the RuntimeHealth/WorkerHealth shape."""
    return {
        "schema_version": 1,
        "mode": "real",
        "engine_lifecycle": "resident",
        "control": {
            "pid": 1234,
            "instance_id": "10676aa6-86e1-424d-a8dd-77f6ce09fc57",
            "python_executable": r"D:\envs\control\python.exe",
            "python_version": "3.11.15",
            "audit_log": r"D:\runtime\logs\x\engine-audit.jsonl",
        },
        "workers": {
            "indextts": {
                "state": "ready",
                "pid": 1001,
                "create_time": 100.0,
                "python_executable": r"D:\envs\index\python.exe",
                "python_version": "3.11.15",
                "source_revision": "90ca4d608209584bad3a5bd5becc0b80c146e60f",
                "active_inference": 0,
                "digest_mismatch": False,
            },
            "gpt_sovits": {
                "state": "ready",
                "pid": 1002,
                "create_time": 200.0,
                "python_executable": r"D:\envs\gsv\python.exe",
                "python_version": "3.11.15",
                "source_revision": "d523079fc05d9a8028d6085bffe4a2757c32abb6",
                "active_inference": 0,
                "digest_mismatch": False,
            },
        },
        "gpu_queue": {
            "state": "accepting",
            "active_count": 0,
            "queued_count": 0,
            "max_active_observed": 1,
            "max_concurrency": 1,
        },
        "engine_lock_sha256": "0" * 64,
        "checkpoint_lock_sha256": "1" * 64,
        "uv_lock_mismatch": False,
        "env_lock_mismatch": False,
        "inventory_mismatch": False,
        "pid_registry_stale": False,
        "model_revision_mismatch": False,
        "cuda": {"available": True, "name": "NVIDIA GeForce RTX 5080", "uuid": "GPU-0000"},
        "source_revisions": {},
    }


@pytest.fixture
def fake_settings(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "app.fake.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 5}
engines:
  indextts:
    base_url: http://127.0.0.1:19871
    python_executable: i.exe
    repo_dir: i
    request_timeout_seconds: 10
  gpt_sovits:
    base_url: http://127.0.0.1:19880
    python_executable: g.exe
    repo_dir: g
    request_timeout_seconds: 10
""",
        encoding="utf-8",
    )
    return load_settings(config)
