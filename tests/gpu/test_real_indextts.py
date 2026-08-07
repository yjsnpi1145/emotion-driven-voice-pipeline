"""Real IndexTTS2 golden gate: reference synthesis on the real GPU engine."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from voice_pipeline.modules.audio.wav_probe import probe_wav


@pytest.mark.gpu
async def test_real_index_reference_is_decodable_and_in_window(
    gpu_settings, zh_ja_request, run_manifest_dir
) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    base_url = f"http://{gpu_settings.server.host}:{gpu_settings.server.port}"
    case_dir = run_manifest_dir / "zh-ja-001"
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "request.json").write_text(
        __import__("json").dumps(zh_ja_request, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payload = {
        "request_id": zh_ja_request["request_id"],
        "base_voice_path": zh_ja_request["base_voice_path"],
        "ref_text_cn": zh_ja_request["ref_text_cn"],
        "emotion_vector": zh_ja_request["emotion_vector"],
        "seed": zh_ja_request["seed"],
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=600) as client:
        resp = await client.post("/api/v1/jobs/reference", json=payload)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        deadline = time.monotonic() + 900
        status = None
        while time.monotonic() < deadline:
            status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
            if status["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.0)
        assert status is not None and status["status"] == "succeeded", status

        download = await client.get(f"/api/v1/jobs/{job_id}/audio/reference")
        assert download.status_code == 200, download.text
        target = case_dir / "reference.wav"
        target.write_bytes(download.content)

    audio = probe_wav(target, require_reference_window=True)
    assert audio.duration_seconds >= 3.0
    assert audio.duration_seconds <= 9.0
    assert audio.rms_dbfs > -50.0
    assert audio.channels == 1
    assert audio.sample_rate == 22050


@pytest.mark.gpu
def test_reference_probe_rejects_silent_audio(gpu_settings, tmp_path: Path) -> None:
    """The fixed -50 dBFS silence threshold must reject a silent reference."""
    import math
    import struct
    import wave

    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        frames = b"".join(
            struct.pack("<h", 0) for _ in range(int(4.0 * 22050))
        )
        wf.writeframes(frames)

    from voice_pipeline.core.errors import ErrorCode, PipelineError

    with pytest.raises(PipelineError) as exc_info:
        probe_wav(silent, require_reference_window=True)
    assert exc_info.value.code == ErrorCode.AUDIO_SILENT


@pytest.mark.gpu
def test_reference_probe_rejects_out_of_window(gpu_settings, tmp_path: Path) -> None:
    from tests.unit.conftest import write_tone

    too_short = tmp_path / "short.wav"
    write_tone(too_short, 2.5)
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    with pytest.raises(PipelineError) as exc_info:
        probe_wav(too_short, require_reference_window=True)
    assert exc_info.value.code == ErrorCode.REFERENCE_DURATION_OUT_OF_RANGE
