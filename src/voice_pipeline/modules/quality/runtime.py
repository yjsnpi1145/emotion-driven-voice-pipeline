from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from voice_pipeline.models.runtime_settings import (
    QualityScoringSettingsUpdate,
    QualityScoringSettingsView,
)
from voice_pipeline.modules.quality.models import QualityReport
from voice_pipeline.modules.quality.ports import QualityAnalyzer


class RuntimeQualityGate:
    """Persisted runtime policy wrapper around the selected quality analyzer."""

    def __init__(self, analyzer: QualityAnalyzer, *, state_dir: Path) -> None:
        self._analyzer = analyzer
        self._state_dir = state_dir.resolve()
        self._settings_path = self._state_dir / "quality-settings.json"
        self._asr_text_scoring_enabled = True
        self._source: Literal["config", "runtime"] = "config"
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            snapshot = await asyncio.to_thread(self._load)
            if snapshot is not None:
                self._asr_text_scoring_enabled = snapshot.asr_text_scoring_enabled
                self._source = "runtime"
            self._started = True

    @property
    def policy_fingerprint(self) -> str:
        if self._asr_text_scoring_enabled:
            return self._analyzer.policy_fingerprint
        return self._disabled_policy_fingerprint

    @property
    def asr_text_scoring_enabled(self) -> bool:
        return self._asr_text_scoring_enabled

    def view(self) -> QualityScoringSettingsView:
        return QualityScoringSettingsView(
            asr_text_scoring_enabled=self._asr_text_scoring_enabled,
            source=self._source,
        )

    async def update(self, request: QualityScoringSettingsUpdate) -> QualityScoringSettingsView:
        async with self._lock:
            await asyncio.to_thread(self._persist, request)
            self._asr_text_scoring_enabled = request.asr_text_scoring_enabled
            self._source = "runtime"
            return self.view()

    async def analyze_reference(self, *, audio_path: Path, expected_text: str) -> QualityReport:
        async with self._lock:
            report = await self._analyzer.analyze_reference(
                audio_path=audio_path,
                expected_text=expected_text,
            )
            if self._asr_text_scoring_enabled:
                return report.model_copy(
                    update={"policy_fingerprint": self._analyzer.policy_fingerprint}
                )
            checks = tuple(
                "text_skipped" if item in {"text", "text_failed"} else item
                for item in report.checks
            )
            vad_failed = any(
                item in {"duration_failed", "speech_failed", "ratio_failed"} for item in checks
            )
            return report.model_copy(
                update={
                    "policy_fingerprint": self._disabled_policy_fingerprint,
                    "passed": not vad_failed,
                    "checks": checks,
                    "failure_code": "QUALITY_VAD_FAILED" if vad_failed else None,
                }
            )

    def accepts_saved_report(self, report: QualityReport) -> bool:
        return report.passed and report.policy_fingerprint in {
            self._analyzer.policy_fingerprint,
            self._disabled_policy_fingerprint,
        }

    @property
    def _disabled_policy_fingerprint(self) -> str:
        payload = {
            "schema_version": 1,
            "base_policy_fingerprint": self._analyzer.policy_fingerprint,
            "asr_text_scoring_enabled": False,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load(self) -> QualityScoringSettingsUpdate | None:
        try:
            return QualityScoringSettingsUpdate.model_validate_json(
                self._settings_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None

    def _persist(self, request: QualityScoringSettingsUpdate) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        partial = self._settings_path.with_name(
            f".{self._settings_path.name}.{os.getpid()}.partial"
        )
        try:
            with open(partial, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, self._settings_path)
        finally:
            partial.unlink(missing_ok=True)
