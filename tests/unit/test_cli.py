from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf
from typer.testing import CliRunner

from voice_pipeline.cli import app
from voice_pipeline.models.schemas import SegmentSynthesisRequest

runner = CliRunner()


def write_tone(path: Path, seconds: float) -> None:
    sample_rate = 22050
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = 0.2 * np.sin(2 * np.pi * 220 * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def test_synthesize_never_falls_back_when_server_is_down(tmp_path) -> None:
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, seconds=5.0)
    request = tmp_path / "request.json"
    request.write_text(
        SegmentSynthesisRequest(
            request_id="d955a4a2-bf44-4a49-a82c-2962eb602d75",
            base_voice_path=base_voice.resolve(),
            ref_text_cn="我依然会向前走。",
            emotion_vector=[0.0, 0.0, 0.2, 0.0, 0.0, 0.2, 0.0, 0.2],
            target_text="I will keep moving forward.",
            target_language="en",
            seed=1234,
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://127.0.0.1:1",
            "--request",
            str(request),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONTROL_PLANE_UNAVAILABLE"
    assert not (tmp_path / "out").exists()


def test_invalid_request_returns_2_without_http(tmp_path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://127.0.0.1:1",
            "--request",
            str(request),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert not (tmp_path / "out").exists()
