from __future__ import annotations

import unicodedata
from typing import Literal

from rapidfuzz.fuzz import ratio

from voice_pipeline.modules.quality.models import QualityMetrics, QualityPolicy, QualityReport


def normalize_reference_text(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
    )


def evaluate_quality(
    *,
    total_seconds: float,
    speech_seconds: float,
    expected_text: str,
    transcript: str,
    policy: QualityPolicy,
) -> QualityReport:
    expected = normalize_reference_text(expected_text)
    observed = normalize_reference_text(transcript)
    similarity = ratio(expected, observed) / 100.0 if expected and observed else 0.0
    speech_ratio = speech_seconds / total_seconds if total_seconds > 0 else 0.0
    return evaluate_quality_metrics(
        metrics=QualityMetrics(
            total_duration_seconds=total_seconds,
            speech_duration_seconds=speech_seconds,
            speech_ratio=speech_ratio,
            expected_text=expected_text,
            transcript=transcript,
            normalized_expected=expected,
            normalized_transcript=observed,
            normalized_text_similarity=similarity,
        ),
        policy=policy,
    )


def evaluate_quality_metrics(*, metrics: QualityMetrics, policy: QualityPolicy) -> QualityReport:
    required_similarity = (
        policy.short_text_min_similarity
        if len(metrics.normalized_expected) <= policy.short_text_max_normalized_length
        else policy.min_similarity
    )
    checks = (
        "duration"
        if policy.min_total_seconds <= metrics.total_duration_seconds <= policy.max_total_seconds
        else "duration_failed",
        "speech"
        if metrics.speech_duration_seconds >= policy.min_speech_seconds
        else "speech_failed",
        "ratio" if metrics.speech_ratio >= policy.min_speech_ratio else "ratio_failed",
        "text" if metrics.normalized_text_similarity >= required_similarity else "text_failed",
    )
    failed = tuple(item for item in checks if item.endswith("_failed"))
    failure_code: Literal["QUALITY_VAD_FAILED", "QUALITY_TEXT_MISMATCH"] | None = (
        "QUALITY_VAD_FAILED"
        if any(item in failed for item in ("duration_failed", "speech_failed", "ratio_failed"))
        else "QUALITY_TEXT_MISMATCH"
        if "text_failed" in failed
        else None
    )
    return QualityReport(
        policy_fingerprint=policy.fingerprint(),
        passed=not failed,
        total_duration_seconds=metrics.total_duration_seconds,
        speech_duration_seconds=metrics.speech_duration_seconds,
        speech_ratio=metrics.speech_ratio,
        speech_timestamps=metrics.speech_timestamps,
        expected_text=metrics.expected_text,
        transcript=metrics.transcript,
        normalized_expected=metrics.normalized_expected,
        normalized_transcript=metrics.normalized_transcript,
        normalized_text_similarity=metrics.normalized_text_similarity,
        detected_language=metrics.detected_language,
        detected_language_probability=metrics.detected_language_probability,
        checks=checks,
        failure_code=failure_code,
    )
