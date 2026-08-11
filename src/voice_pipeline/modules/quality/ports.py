from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from voice_pipeline.modules.quality.models import QualityReport


@runtime_checkable
class QualityAnalyzer(Protocol):
    @property
    def policy_fingerprint(self) -> str: ...

    async def analyze_reference(self, *, audio_path: Path, expected_text: str) -> QualityReport: ...


@runtime_checkable
class SavedQualityReportValidator(Protocol):
    def accepts_saved_report(self, report: QualityReport) -> bool: ...
