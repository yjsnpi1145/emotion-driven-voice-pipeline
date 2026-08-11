from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_pipeline.models.runtime_settings import QualityScoringSettingsUpdate
from voice_pipeline.modules.quality.models import QualityPolicy, QualityReport
from voice_pipeline.modules.quality.runtime import RuntimeQualityGate
from voice_pipeline.modules.quality.text import evaluate_quality


class StaticQualityAnalyzer:
    def __init__(self, report: QualityReport) -> None:
        self.report = report
        self.calls = 0

    @property
    def policy_fingerprint(self) -> str:
        return self.report.policy_fingerprint

    async def analyze_reference(self, *, audio_path: Path, expected_text: str) -> QualityReport:
        del audio_path, expected_text
        self.calls += 1
        return self.report


def _text_failure() -> QualityReport:
    return evaluate_quality(
        total_seconds=4.0,
        speech_seconds=3.5,
        expected_text="这是预期文本",
        transcript="完全不同的话",
        policy=QualityPolicy(),
    )


def _vad_failure() -> QualityReport:
    return evaluate_quality(
        total_seconds=4.0,
        speech_seconds=0.5,
        expected_text="这是预期文本",
        transcript="这是预期文本",
        policy=QualityPolicy(),
    )


@pytest.mark.asyncio
async def test_runtime_quality_gate_disables_only_text_scoring_and_persists(
    tmp_path: Path,
) -> None:
    underlying = StaticQualityAnalyzer(_text_failure())
    gate = RuntimeQualityGate(underlying, state_dir=tmp_path / "state")
    await gate.start()

    strict = await gate.analyze_reference(
        audio_path=tmp_path / "unused.wav", expected_text="这是预期文本"
    )
    assert gate.view().asr_text_scoring_enabled is True
    assert gate.view().source == "config"
    assert strict.passed is False
    assert strict.failure_code == "QUALITY_TEXT_MISMATCH"
    assert strict.policy_fingerprint == underlying.policy_fingerprint

    disabled_view = await gate.update(
        QualityScoringSettingsUpdate(asr_text_scoring_enabled=False)
    )
    relaxed = await gate.analyze_reference(
        audio_path=tmp_path / "unused.wav", expected_text="这是预期文本"
    )

    assert disabled_view.asr_text_scoring_enabled is False
    assert disabled_view.source == "runtime"
    assert relaxed.passed is True
    assert relaxed.failure_code is None
    assert relaxed.checks[-1] == "text_skipped"
    assert relaxed.transcript == strict.transcript
    assert relaxed.normalized_text_similarity == strict.normalized_text_similarity
    assert relaxed.policy_fingerprint != underlying.policy_fingerprint
    assert gate.accepts_saved_report(strict) is False
    assert gate.accepts_saved_report(relaxed) is True
    saved = json.loads((tmp_path / "state" / "quality-settings.json").read_text("utf-8"))
    assert saved == {"schema_version": 1, "asr_text_scoring_enabled": False}
    assert not list((tmp_path / "state").glob("*.tmp"))

    restored = RuntimeQualityGate(underlying, state_dir=tmp_path / "state")
    await restored.start()
    assert restored.view().asr_text_scoring_enabled is False
    assert restored.view().source == "runtime"


@pytest.mark.asyncio
async def test_runtime_quality_gate_keeps_vad_failure_when_text_scoring_is_disabled(
    tmp_path: Path,
) -> None:
    underlying = StaticQualityAnalyzer(_vad_failure())
    gate = RuntimeQualityGate(underlying, state_dir=tmp_path / "state")
    await gate.start()
    await gate.update(QualityScoringSettingsUpdate(asr_text_scoring_enabled=False))

    report = await gate.analyze_reference(
        audio_path=tmp_path / "unused.wav", expected_text="这是预期文本"
    )

    assert report.passed is False
    assert report.failure_code == "QUALITY_VAD_FAILED"
    assert "speech_failed" in report.checks
    assert "ratio_failed" in report.checks
    assert "text_skipped" in report.checks


@pytest.mark.asyncio
async def test_runtime_quality_gate_keeps_versions_valid_across_toggle_changes(
    tmp_path: Path,
) -> None:
    underlying = StaticQualityAnalyzer(_text_failure())
    gate = RuntimeQualityGate(underlying, state_dir=tmp_path / "state")
    await gate.start()
    strict_pass = _vad_failure().model_copy(
        update={
            "passed": True,
            "checks": ("duration", "speech", "ratio", "text"),
            "failure_code": None,
        }
    )
    await gate.update(QualityScoringSettingsUpdate(asr_text_scoring_enabled=False))
    relaxed_pass = await gate.analyze_reference(
        audio_path=tmp_path / "unused.wav", expected_text="这是预期文本"
    )
    await gate.update(QualityScoringSettingsUpdate(asr_text_scoring_enabled=True))

    assert gate.accepts_saved_report(strict_pass) is True
    assert gate.accepts_saved_report(relaxed_pass) is True
    assert gate.accepts_saved_report(_text_failure()) is False
