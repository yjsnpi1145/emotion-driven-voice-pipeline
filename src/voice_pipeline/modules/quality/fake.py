from __future__ import annotations

from pathlib import Path

from voice_pipeline.modules.audio.wav_probe import probe_wav
from voice_pipeline.modules.quality.models import QualityPolicy, QualityReport
from voice_pipeline.modules.quality.text import evaluate_quality


class DeterministicQualityAnalyzer:
    def __init__(self, policy: QualityPolicy | None = None) -> None:
        self.policy = policy or QualityPolicy()

    @property
    def policy_fingerprint(self) -> str:
        return self.policy.fingerprint()

    async def analyze_reference(self, *, audio_path: Path, expected_text: str) -> QualityReport:
        audio = probe_wav(audio_path, require_reference_window=True)
        return evaluate_quality(
            total_seconds=audio.duration_seconds,
            speech_seconds=audio.duration_seconds,
            expected_text=expected_text,
            transcript=expected_text,
            policy=self.policy,
        )
