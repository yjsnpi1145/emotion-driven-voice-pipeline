from __future__ import annotations

import os
from pathlib import Path

import pytest

from voice_pipeline.modules.quality.faster_whisper import FasterWhisperQualityAnalyzer


def _quality_test_inputs() -> tuple[Path, Path, Path, str]:
    root = Path(__file__).resolve().parents[2]
    sample = os.environ.get("VOICE_PIPELINE_QUALITY_SAMPLE_WAV")
    expected_text = os.environ.get("VOICE_PIPELINE_QUALITY_SAMPLE_TEXT")
    if not sample or not expected_text:
        pytest.fail(
            "quality model gate requires VOICE_PIPELINE_QUALITY_SAMPLE_WAV and "
            "VOICE_PIPELINE_QUALITY_SAMPLE_TEXT"
        )
    return (
        root / "runtime" / "models" / "faster-whisper-small",
        root / "config" / "quality-model.lock.yaml",
        Path(sample).resolve(),
        expected_text,
    )


@pytest.mark.asyncio
@pytest.mark.quality_model
async def test_pinned_faster_whisper_model_accepts_known_chinese_reference() -> None:
    model_path, lock_path, audio_path, expected_text = _quality_test_inputs()
    analyzer = FasterWhisperQualityAnalyzer(
        model_path=model_path,
        model_lock_path=lock_path,
    )
    report = await analyzer.analyze_reference(
        audio_path=audio_path,
        expected_text=expected_text,
    )
    assert report.passed, report.model_dump(mode="json")
