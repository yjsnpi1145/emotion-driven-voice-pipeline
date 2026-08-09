from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file


def write_tone(
    path: Path,
    seconds: float,
    amplitude: float = 0.2,
    sample_rate: int = 22050,
    channels: int = 1,
) -> None:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = amplitude * np.sin(2 * np.pi * 220 * t)
    if channels == 2:
        data = np.stack([data, data], axis=1)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def test_reference_must_be_between_three_and_ten_seconds(tmp_path: Path) -> None:
    short = tmp_path / "short.wav"
    write_tone(short, 2.9)
    with pytest.raises(PipelineError, match="REFERENCE_DURATION_OUT_OF_RANGE"):
        probe_wav(short, require_reference_window=True)


def test_reference_between_nine_and_ten_seconds_is_accepted(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    write_tone(reference, 9.358)

    result = probe_wav(reference, require_reference_window=True)

    assert result.duration_seconds == pytest.approx(9.358, abs=0.01)


def test_reference_longer_than_ten_seconds_rejected(tmp_path: Path) -> None:
    long_wav = tmp_path / "long.wav"
    write_tone(long_wav, 10.1)
    with pytest.raises(PipelineError, match="REFERENCE_DURATION_OUT_OF_RANGE"):
        probe_wav(long_wav, require_reference_window=True)


def test_rejects_silent_wav(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    sf.write(silent, np.zeros(22050 * 4, dtype=np.float32), 22050)
    with pytest.raises(PipelineError, match="AUDIO_SILENT"):
        probe_wav(silent, require_reference_window=True)


def test_rejects_stereo_wav(tmp_path: Path) -> None:
    stereo = tmp_path / "stereo.wav"
    write_tone(stereo, 4.0, channels=2)
    with pytest.raises(PipelineError, match="INVALID_AUDIO"):
        probe_wav(stereo, require_reference_window=True)


def test_rejects_nan_samples(tmp_path: Path) -> None:
    bad = tmp_path / "nan.wav"
    data = np.full(22050 * 4, np.nan, dtype=np.float32)
    sf.write(bad, data, 22050, subtype="FLOAT")
    with pytest.raises(PipelineError, match="INVALID_AUDIO"):
        probe_wav(bad, require_reference_window=True)


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="INVALID_AUDIO"):
        probe_wav(tmp_path / "missing.wav", require_reference_window=True)


def test_valid_reference_returns_audio_result(tmp_path: Path) -> None:
    good = tmp_path / "good.wav"
    write_tone(good, 4.0)
    result = probe_wav(good, require_reference_window=True)
    assert result.duration_seconds == pytest.approx(4.0, abs=0.01)
    assert result.channels == 1
    assert result.sample_rate == 22050
    assert len(result.content_sha256) == 64
    assert result.rms_dbfs > -50.0


def test_target_must_exceed_tenth_second(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.wav"
    write_tone(tiny, 0.05)
    with pytest.raises(PipelineError, match="INVALID_AUDIO"):
        probe_wav(tiny, require_reference_window=False)


def test_sha256_file_changes_when_content_changes(tmp_path: Path) -> None:
    weight = tmp_path / "model.pth"
    weight.write_bytes(b"a")
    first = sha256_file(weight)
    weight.write_bytes(b"b")
    assert sha256_file(weight) != first
