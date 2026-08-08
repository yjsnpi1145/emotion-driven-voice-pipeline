from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from voice_pipeline.models.schemas import StrictModel


class QualityPolicy(StrictModel):
    schema_version: Literal[1] = 1
    min_total_seconds: float = 3.0
    max_total_seconds: float = 9.0
    min_speech_seconds: float = 1.5
    min_speech_ratio: float = 0.35
    min_similarity: float = 0.60
    short_text_max_normalized_length: int = Field(default=3, ge=0)
    short_text_min_similarity: float = 0.75
    normalizer_version: Literal[1] = 1

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class QualityReport(StrictModel):
    schema_version: Literal[1] = 1
    policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    total_duration_seconds: float = Field(allow_inf_nan=False)
    speech_duration_seconds: float = Field(allow_inf_nan=False)
    speech_ratio: float = Field(allow_inf_nan=False)
    speech_timestamps: tuple[SpeechInterval, ...] = ()
    expected_text: str
    transcript: str
    normalized_expected: str
    normalized_transcript: str
    normalized_text_similarity: float = Field(allow_inf_nan=False)
    detected_language: str | None = None
    detected_language_probability: float | None = Field(default=None, allow_inf_nan=False)
    checks: tuple[str, ...]
    failure_code: Literal["QUALITY_VAD_FAILED", "QUALITY_TEXT_MISMATCH"] | None = None


class SpeechInterval(StrictModel):
    start_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0.0, allow_inf_nan=False)


class QualityMetrics(StrictModel):
    total_duration_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    speech_duration_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    speech_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    expected_text: str
    transcript: str
    normalized_expected: str
    normalized_transcript: str
    normalized_text_similarity: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    speech_timestamps: tuple[SpeechInterval, ...] = ()
    detected_language: str | None = None
    detected_language_probability: float | None = Field(
        default=None, ge=0.0, le=1.0, allow_inf_nan=False
    )
