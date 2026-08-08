from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import yaml

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file
from voice_pipeline.modules.quality.models import (
    QualityMetrics,
    QualityPolicy,
    QualityReport,
    SpeechInterval,
)
from voice_pipeline.modules.quality.text import (
    evaluate_quality_metrics,
    normalize_reference_text,
)


def _create_whisper_model(*, model_path: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    return WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        local_files_only=True,
    )


class FasterWhisperQualityAnalyzer:
    """Thin adapter around the pinned upstream faster-whisper VAD/ASR APIs."""

    def __init__(
        self,
        *,
        model_path: Path,
        model_lock_path: Path | None = None,
        policy: QualityPolicy | None = None,
        model_factory: Callable[..., Any] = _create_whisper_model,
    ) -> None:
        if not model_path.is_dir():
            raise PipelineError(
                ErrorCode.QUALITY_MODEL_UNAVAILABLE,
                "quality",
                f"local faster-whisper model directory is unavailable: {model_path}",
                retryable=False,
            )
        self._model_path = model_path.resolve()
        self._model_lock_path = model_lock_path.resolve() if model_lock_path is not None else None
        self._locked_model_files = self._validate_model_lock()
        self._policy = policy or QualityPolicy()
        self._model_factory = model_factory
        self._model: Any | None = None
        self._policy_fingerprint: str | None = None

    @property
    def policy_fingerprint(self) -> str:
        if self._policy_fingerprint is None:
            payload = {
                "schema_version": 1,
                "policy": self._policy.model_dump(mode="json"),
                "faster_whisper": version("faster-whisper"),
                "rapidfuzz": version("RapidFuzz"),
                "model_files": self._locked_model_files or self._model_files(),
                "asr": {
                    "device": "cpu",
                    "compute_type": "int8",
                    "language": "zh",
                    "beam_size": 5,
                    "condition_on_previous_text": False,
                    "vad_filter": True,
                },
            }
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self._policy_fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self._policy_fingerprint

    async def analyze_reference(self, *, audio_path: Path, expected_text: str) -> QualityReport:
        audio = probe_wav(audio_path, require_reference_window=True)
        transcript, intervals, language, language_probability = await asyncio.to_thread(
            self._transcribe, audio_path
        )
        clipped_intervals = _clip_intervals(intervals, total_seconds=audio.duration_seconds)
        expected = normalize_reference_text(expected_text)
        observed = normalize_reference_text(transcript)
        similarity = _similarity(expected, observed)
        speech_seconds = _union_duration(clipped_intervals)
        report = evaluate_quality_metrics(
            metrics=QualityMetrics(
                total_duration_seconds=audio.duration_seconds,
                speech_duration_seconds=speech_seconds,
                speech_ratio=speech_seconds / audio.duration_seconds,
                expected_text=expected_text,
                transcript=transcript,
                normalized_expected=expected,
                normalized_transcript=observed,
                normalized_text_similarity=similarity,
                speech_timestamps=tuple(clipped_intervals),
                detected_language=language,
                detected_language_probability=language_probability,
            ),
            policy=self._policy,
        )
        return report.model_copy(update={"policy_fingerprint": self.policy_fingerprint})

    def _transcribe(
        self, audio_path: Path
    ) -> tuple[str, list[SpeechInterval], str | None, float | None]:
        if self._model is None:
            self._model = self._model_factory(
                model_path=str(self._model_path), device="cpu", compute_type="int8"
            )
        segments, info = self._model.transcribe(
            str(audio_path),
            language="zh",
            beam_size=5,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        return _result_from_segments(segments, info)

    def _model_files(self) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        for path in sorted(self._model_path.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files.append(
                    {
                        "relative_path": path.relative_to(self._model_path).as_posix(),
                        "sha256": sha256_file(path),
                    }
                )
        return files

    def _validate_model_lock(self) -> list[dict[str, str]]:
        if self._model_lock_path is None:
            return []
        if not self._model_lock_path.is_file() or self._model_lock_path.is_symlink():
            raise PipelineError(
                ErrorCode.QUALITY_MODEL_UNAVAILABLE,
                "quality",
                "quality model lock is unavailable",
                retryable=False,
            )
        try:
            raw = yaml.safe_load(self._model_lock_path.read_text(encoding="utf-8"))
            files = raw["files"]
            if raw["schema_version"] != 1 or not isinstance(files, list):
                raise ValueError("invalid schema")
        except (OSError, TypeError, ValueError, KeyError, yaml.YAMLError) as exc:
            raise PipelineError(
                ErrorCode.QUALITY_MODEL_UNAVAILABLE,
                "quality",
                "quality model lock is invalid",
                retryable=False,
            ) from exc

        locked: list[dict[str, str]] = []
        for item in files:
            try:
                relative = Path(str(item["path"]))
                expected_size = int(item["size"])
                expected_sha256 = str(item["sha256"])
            except (TypeError, ValueError, KeyError) as exc:
                raise PipelineError(
                    ErrorCode.QUALITY_MODEL_UNAVAILABLE,
                    "quality",
                    "quality model lock has an invalid file entry",
                    retryable=False,
                ) from exc
            candidate = (self._model_path / relative).resolve()
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not _is_within(candidate, self._model_path)
                or not candidate.is_file()
                or candidate.is_symlink()
                or candidate.stat().st_size != expected_size
                or sha256_file(candidate) != expected_sha256
            ):
                raise PipelineError(
                    ErrorCode.QUALITY_MODEL_UNAVAILABLE,
                    "quality",
                    f"quality model file does not match lock: {relative.as_posix()}",
                    retryable=False,
                )
            locked.append({"relative_path": relative.as_posix(), "sha256": expected_sha256})
        return locked


def _similarity(expected: str, observed: str) -> float:
    from rapidfuzz.fuzz import ratio

    return ratio(expected, observed) / 100.0 if expected and observed else 0.0


def _result_from_segments(
    segments: Iterable[Any], info: Any
) -> tuple[str, list[SpeechInterval], str | None, float | None]:
    collected = list(segments)
    intervals = [
        SpeechInterval(start_seconds=float(segment.start), end_seconds=float(segment.end))
        for segment in collected
        if float(segment.end) > float(segment.start)
    ]
    transcript = "".join(str(segment.text) for segment in collected).strip()
    language = getattr(info, "language", None)
    probability = getattr(info, "language_probability", None)
    return (
        transcript,
        intervals,
        str(language) if language is not None else None,
        (float(probability) if probability is not None else None),
    )


def _clip_intervals(
    intervals: Iterable[SpeechInterval], *, total_seconds: float
) -> list[SpeechInterval]:
    clipped: list[SpeechInterval] = []
    for interval in intervals:
        start = min(max(interval.start_seconds, 0.0), total_seconds)
        end = min(max(interval.end_seconds, start), total_seconds)
        if end > start:
            clipped.append(SpeechInterval(start_seconds=start, end_seconds=end))
    return clipped


def _union_duration(intervals: Iterable[SpeechInterval]) -> float:
    merged: list[tuple[float, float]] = []
    for interval in sorted(intervals, key=lambda item: (item.start_seconds, item.end_seconds)):
        if not merged or interval.start_seconds > merged[-1][1]:
            merged.append((interval.start_seconds, interval.end_seconds))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], interval.end_seconds))
    return sum(end - start for start, end in merged)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
