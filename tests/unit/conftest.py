from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.models.schemas import (
    EngineFingerprint,
    EngineIdentity,
    ExecutionContext,
    RuntimeHealth,
    SegmentSynthesisRequest,
    WorkerHealth,
    WorkersHealth,
)


def fake_fingerprint(engine: str) -> EngineFingerprint:
    import hashlib

    source = "in-process-fake"

    def h(field: str) -> str:
        return hashlib.sha256(f"{engine}:{source}:{field}".encode()).hexdigest()

    return EngineFingerprint(
        schema_version=1,
        engine=engine,  # type: ignore[arg-type]
        source_revision=source,
        model_revision="1",
        engine_lock_sha256=h("engine-lock"),
        checkpoint_lock_sha256=h("checkpoint-lock"),
        environment_lock_sha256=h("environment-lock"),
        runtime_config_sha256=h("runtime-config"),
    )


def write_tone(
    path: Path,
    seconds: float,
    amplitude: float = 0.2,
    sample_rate: int = 22050,
    frequency: float = 220.0,
) -> None:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = amplitude * np.sin(2 * np.pi * frequency * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def make_request(tmp_path: Path) -> SegmentSynthesisRequest:
    return SegmentSynthesisRequest(
        request_id="cf2deece-f4e8-4114-954b-bfc907730e01",
        base_voice_path=(tmp_path / "音色 voice.wav").resolve(),
        ref_text_cn="我已经失去了一切，可我仍然活着。",
        emotion_vector=[0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        target_text="私はすべてを失った。それでも、まだ生きている。",
        target_language="ja",
        seed=1234,
    )


def make_context(
    tmp_path: Path, request_id: str | UUID, job_id: UUID | None = None
) -> ExecutionContext:
    job = job_id or uuid4()
    rid = request_id if isinstance(request_id, UUID) else UUID(request_id)
    return ExecutionContext(
        job_id=job,
        request_id=rid,
        job_dir=tmp_path / "jobs" / str(job),
    )


class RecordingIndexClient:
    def __init__(self, calls: list[tuple[str, object]], duration_seconds: float = 4.0):
        self.calls = calls
        self.duration_seconds = duration_seconds

    async def synthesize(self, request, output_path: Path):
        self.calls.append(("index", request))
        write_tone(output_path, self.duration_seconds)
        from voice_pipeline.modules.audio.wav_probe import probe_wav

        return probe_wav(output_path, require_reference_window=False)

    def fingerprint(self) -> EngineFingerprint:
        return fake_fingerprint("indextts")


class RecordingGsvClient:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls

    async def synthesize(self, request, output_path: Path):
        self.calls.append(("gsv", request))
        write_tone(output_path, 1.5, sample_rate=32000)
        from voice_pipeline.modules.audio.wav_probe import probe_wav

        return probe_wav(output_path, require_reference_window=False)

    def fingerprint(self) -> EngineFingerprint:
        return fake_fingerprint("gpt_sovits")


class RecordingInferenceLease:
    def __init__(self, calls: list[tuple[str, object]], engine: str):
        self.calls = calls
        self.engine = engine

    async def confirm_completed(self) -> None:
        return None

    async def confirm_aborted(self) -> None:
        return None

    async def mark_unknown(self) -> None:
        return None


class RecordingEngineRuntime:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls
        self._fingerprints = {
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        }

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def stop(self, *, deadline: float | None = None) -> None:
        self.calls.append(("stop", None))

    async def ensure_engine(self, engine: str) -> None:
        self.calls.append((f"ensure:{engine}", engine))

    async def abort_engine(
        self, engine: str, *, reason: str, deadline: float | None = None
    ) -> None:
        self.calls.append((f"abort:{engine}", reason))

    def engine_identity(self, engine: str) -> EngineIdentity:
        return EngineIdentity(
            worker=engine,  # type: ignore[arg-type]
            pid=os.getpid(),
            create_time=1.0,
            python_executable=Path("D:/fake/python.exe"),
            fingerprint=self._fingerprints[engine],
        )

    def health(self) -> RuntimeHealth:
        workers: dict[str, WorkerHealth] = {}
        for engine in ("indextts", "gpt_sovits"):
            fp = self._fingerprints[engine]
            workers[engine] = WorkerHealth(
                state="ready",
                pid=os.getpid(),
                create_time=1.0,
                python_executable=Path("D:/fake/python.exe"),
                python_version="3.11",
                source_revision="in-process-fake",
                fingerprint=fp,
                preflight_ok=True,
                active_inference=0,
            )
        return RuntimeHealth(
            status="ready",
            workers=WorkersHealth(**workers),  # type: ignore[arg-type]
        )

    async def begin_inference(self, engine: str, *, job_id: UUID):
        return RecordingInferenceLease(self.calls, engine)


class RecordingAuditWriter:
    def __init__(self, events: list[dict[str, object]]):
        self.events = events

    def write(self, **kwargs: object) -> None:
        self.events.append(dict(kwargs))


@pytest.fixture
def index_request(tmp_path):
    from voice_pipeline.models.schemas import IndexSynthesisRequest

    return IndexSynthesisRequest(
        request_id="d613a571-1d69-4f6e-a1b7-3222f61657b8",
        text="我已经失去了一切，可我仍然活着。",
        speaker_audio_path=(tmp_path / "voice.wav").resolve(),
        emotion_vector=[0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        seed=1234,
    )


@pytest.fixture
def gsv_request(tmp_path):
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
            engine_fingerprint=fake_fingerprint("indextts"),
        ),
        text="私はまだ生きている。",
        text_lang="ja",
        seed=1234,
    )
