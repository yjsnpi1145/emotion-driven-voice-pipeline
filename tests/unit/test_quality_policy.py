import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from voice_pipeline.modules.quality.fake import DeterministicQualityAnalyzer
from voice_pipeline.modules.quality.faster_whisper import FasterWhisperQualityAnalyzer
from voice_pipeline.modules.quality.models import QualityMetrics, QualityPolicy
from voice_pipeline.modules.quality.ports import QualityAnalyzer
from voice_pipeline.modules.quality.text import (
    evaluate_quality,
    evaluate_quality_metrics,
    normalize_reference_text,
)


def test_normalize_reference_text_is_unicode_deterministic() -> None:
    assert normalize_reference_text(" Ａ，我 还活着！\r\n") == "a我还活着"


def test_quality_accepts_equivalent_simplified_and_traditional_chinese() -> None:
    report = evaluate_quality(
        total_seconds=5.6076,
        speech_seconds=5.6076,
        expected_text="明明我应该很生气，可被你这样托着，我却一句话都说不出来……",
        transcript="明明我應該很生氣可被你這樣拖著我卻一句話都說不出來",
        policy=QualityPolicy(),
    )

    assert report.passed is True
    assert report.normalized_text_similarity >= 0.60


@pytest.mark.parametrize(
    ("speech_seconds", "speech_ratio", "similarity", "passed"),
    [
        (1.50, 0.35, 0.60, True),
        (1.49, 0.35, 0.60, False),
        (1.50, 0.34, 0.60, False),
        (1.50, 0.35, 0.59, False),
    ],
)
def test_quality_policy_boundaries(
    speech_seconds: float, speech_ratio: float, similarity: float, passed: bool
) -> None:
    policy = QualityPolicy()
    report = evaluate_quality_metrics(
        metrics=QualityMetrics(
            total_duration_seconds=5.0,
            speech_duration_seconds=speech_seconds,
            speech_ratio=speech_ratio,
            expected_text="我还活着",
            transcript="我还活着",
            normalized_expected="我还活着",
            normalized_transcript="我还活着",
            normalized_text_similarity=similarity,
        ),
        policy=policy,
    )
    assert report.passed is passed
    assert report.policy_fingerprint == policy.fingerprint()


@pytest.mark.parametrize("duration", [3.0, 10.0])
def test_quality_policy_duration_closed_boundaries(duration: float) -> None:
    policy = QualityPolicy()
    report = evaluate_quality_metrics(
        metrics=QualityMetrics(
            total_duration_seconds=duration,
            speech_duration_seconds=1.5,
            speech_ratio=0.35,
            expected_text="我还活着",
            transcript="我还活着",
            normalized_expected="我还活着",
            normalized_transcript="我还活着",
            normalized_text_similarity=0.60,
        ),
        policy=policy,
    )
    assert report.passed is True


def test_short_reference_text_requires_stricter_similarity() -> None:
    report = evaluate_quality_metrics(
        metrics=QualityMetrics(
            total_duration_seconds=5.0,
            speech_duration_seconds=2.0,
            speech_ratio=0.5,
            expected_text="好",
            transcript="号",
            normalized_expected="好",
            normalized_transcript="号",
            normalized_text_similarity=0.70,
        ),
        policy=QualityPolicy(),
    )
    assert report.passed is False
    assert "text_failed" in report.checks


def test_quality_metrics_reject_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        QualityMetrics(
            total_duration_seconds=math.nan,
            speech_duration_seconds=1.5,
            speech_ratio=0.5,
            expected_text="我还活着",
            transcript="我还活着",
            normalized_expected="我还活着",
            normalized_transcript="我还活着",
            normalized_text_similarity=1.0,
        )


def test_deterministic_analyzer_implements_quality_protocol() -> None:
    analyzer = DeterministicQualityAnalyzer()
    assert isinstance(analyzer, QualityAnalyzer)
    assert analyzer.policy_fingerprint == analyzer.policy.fingerprint()


@pytest.mark.asyncio
async def test_faster_whisper_adapter_uses_local_cpu_vad_and_asr(tmp_path: Path) -> None:
    from tests.unit.conftest import write_tone

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    audio_path = tmp_path / "reference.wav"
    write_tone(audio_path, seconds=4.0)
    calls: list[tuple[str, object]] = []

    class Model:
        def transcribe(self, path: str, **kwargs: object):
            calls.append((path, kwargs))
            return (
                iter([SimpleNamespace(text="我还活着", start=0.0, end=3.5)]),
                SimpleNamespace(language="zh", language_probability=0.99),
            )

    analyzer = FasterWhisperQualityAnalyzer(
        model_path=model_path,
        model_factory=lambda **kwargs: Model(),
    )
    report = await analyzer.analyze_reference(audio_path=audio_path, expected_text="我还活着")

    assert report.passed is True
    assert report.transcript == "我还活着"
    assert report.detected_language == "zh"
    assert report.speech_timestamps[0].end_seconds == 3.5
    assert calls == [
        (
            str(audio_path),
            {
                "language": "zh",
                "beam_size": 5,
                "condition_on_previous_text": False,
                "vad_filter": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_faster_whisper_adapter_clips_asr_intervals_to_wav_duration(tmp_path: Path) -> None:
    from tests.unit.conftest import write_tone

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    audio_path = tmp_path / "reference.wav"
    write_tone(audio_path, seconds=4.0)

    class Model:
        def transcribe(self, path: str, **kwargs: object):
            return (
                iter([SimpleNamespace(text="我还活着", start=0.0, end=4.1)]),
                SimpleNamespace(language="zh", language_probability=0.99),
            )

    analyzer = FasterWhisperQualityAnalyzer(
        model_path=model_path,
        model_factory=lambda **kwargs: Model(),
    )
    report = await analyzer.analyze_reference(audio_path=audio_path, expected_text="我还活着")

    assert report.speech_duration_seconds == pytest.approx(4.0)
    assert report.speech_ratio == 1.0
    assert report.speech_timestamps[0].end_seconds == pytest.approx(4.0)


def test_faster_whisper_adapter_rejects_model_that_does_not_match_lock(tmp_path: Path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    lock_path = tmp_path / "quality-model.lock.yaml"
    lock_path.write_text(
        """schema_version: 1
repository: Systran/faster-whisper-small
revision: pinned
license_spdx: MIT
files:
  - path: config.json
    size: 2
    sha256: 0000000000000000000000000000000000000000000000000000000000000000
""",
        encoding="utf-8",
    )

    with pytest.raises(PipelineError) as exc_info:
        FasterWhisperQualityAnalyzer(model_path=model_path, model_lock_path=lock_path)

    assert exc_info.value.code == ErrorCode.QUALITY_MODEL_UNAVAILABLE
