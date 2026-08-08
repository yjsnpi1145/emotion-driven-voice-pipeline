from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.models.persistence import OutputAudioSpec
from voice_pipeline.modules.audio.composer import ComposeInput, compose_final


def _tone(path: Path, *, seconds: float) -> None:
    sample_rate = 22_050
    time = np.arange(round(sample_rate * seconds)) / sample_rate
    sf.write(path, (0.2 * np.sin(2 * np.pi * 220 * time)).astype(np.float32), sample_rate)


def _input(path: Path, *, ordinal: int, pause_after_ms: int, state: str = "ready") -> ComposeInput:
    return ComposeInput(
        ordinal=ordinal,
        segment_id=uuid4(),
        gsv_version_id=uuid4(),
        gsv_content_sha256="a" * 64,
        blob_path=path,
        pause_after_ms=pause_after_ms,
        state=state,
    )


def test_composer_appends_only_nonfinal_pause_and_emits_timeline(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _tone(first, seconds=1.0)
    _tone(second, seconds=2.0)

    result = compose_final(
        ordered_inputs=(
            _input(first, ordinal=0, pause_after_ms=500),
            _input(second, ordinal=1, pause_after_ms=999),
        ),
        output_spec=OutputAudioSpec(sample_rate=22_050),
        output_path=tmp_path / "final.wav",
        timeline_path=tmp_path / "timeline.json",
    )

    assert result.audio.duration_seconds == pytest.approx(3.5, abs=0.02)
    assert result.timeline.segments[0].end_seconds == pytest.approx(1.0, abs=0.02)
    assert result.timeline.segments[1].start_seconds == pytest.approx(1.5, abs=0.02)
    assert (tmp_path / "timeline.json").is_file()


def test_composer_rejects_nonready_input_before_creating_outputs(tmp_path: Path) -> None:
    missing = tmp_path / "missing.wav"

    with pytest.raises(PipelineError, match="cannot compose"):
        compose_final(
            ordered_inputs=(_input(missing, ordinal=0, pause_after_ms=0, state="missing"),),
            output_spec=OutputAudioSpec(sample_rate=22_050),
            output_path=tmp_path / "final.wav",
            timeline_path=tmp_path / "timeline.json",
        )

    assert not (tmp_path / "final.wav").exists()
    assert not (tmp_path / "timeline.json").exists()
