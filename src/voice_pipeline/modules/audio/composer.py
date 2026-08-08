from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Literal
from uuid import UUID

import numpy as np
import soundfile as sf
from pydantic import Field

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.chapter import ChapterTimeline, ChapterTimelineSegment
from voice_pipeline.models.persistence import OutputAudioSpec
from voice_pipeline.models.schemas import AudioResult, StrictModel
from voice_pipeline.modules.audio.atomic_output import OutputReservation, reserve_output_path
from voice_pipeline.modules.audio.wav_probe import probe_wav


class ComposeInput(StrictModel):
    ordinal: int = Field(ge=0)
    segment_id: UUID
    gsv_version_id: UUID
    gsv_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blob_path: Path
    pause_after_ms: int = Field(ge=0, le=30_000)
    state: Literal["ready", "deleting", "missing", "corrupt", "deleted"] = "ready"


class ComposedChapterAudio(StrictModel):
    audio: AudioResult
    timeline: ChapterTimeline
    timeline_path: Path


def compose_final(
    *,
    ordered_inputs: tuple[ComposeInput, ...],
    output_spec: OutputAudioSpec,
    output_path: Path,
    timeline_path: Path,
) -> ComposedChapterAudio:
    _validate_inputs(ordered_inputs)
    sample_rate = output_spec.sample_rate or _sample_rate_of(ordered_inputs[0].blob_path)
    waveform, timeline = _build_waveform(ordered_inputs, sample_rate)
    audio_reservation = reserve_output_path(output_path)
    try:
        timeline_reservation = reserve_output_path(timeline_path)
    except BaseException:
        audio_reservation.rollback()
        raise
    audio_partial = audio_reservation.path.with_name(
        f".{audio_reservation.path.stem}.{uuid.uuid4()}.partial.wav"
    )
    timeline_partial = timeline_reservation.path.with_name(
        f".{timeline_reservation.path.stem}.{uuid.uuid4()}.partial.json"
    )
    try:
        sf.write(audio_partial, waveform, sample_rate, subtype="PCM_16")
        audio = probe_wav(audio_partial, require_reference_window=False)
        _write_json(timeline_partial, timeline.model_dump(mode="json"))
        audio_reservation.publish(audio_partial)
        timeline_reservation.publish(timeline_partial)
        return ComposedChapterAudio(
            audio=audio.model_copy(update={"path": audio_reservation.path}),
            timeline=timeline,
            timeline_path=timeline_reservation.path,
        )
    except BaseException:
        _rollback_if_active(audio_reservation)
        _rollback_if_active(timeline_reservation)
        raise
    finally:
        audio_partial.unlink(missing_ok=True)
        timeline_partial.unlink(missing_ok=True)


def _validate_inputs(ordered_inputs: tuple[ComposeInput, ...]) -> None:
    if not ordered_inputs:
        raise PipelineError(
            ErrorCode.VERSION_NOT_READY,
            "composer",
            "cannot compose an empty chapter",
            retryable=False,
        )
    for ordinal, item in enumerate(ordered_inputs):
        if item.ordinal != ordinal or item.state != "ready" or not item.blob_path.is_file():
            raise PipelineError(
                ErrorCode.VERSION_NOT_READY,
                "composer",
                "cannot compose because a selected GSV version is not ready",
                retryable=False,
                details={"ordinal": item.ordinal, "segment_id": str(item.segment_id)},
            )


def _build_waveform(
    ordered_inputs: tuple[ComposeInput, ...], sample_rate: int
) -> tuple[np.ndarray, ChapterTimeline]:
    parts: list[np.ndarray] = []
    entries: list[ChapterTimelineSegment] = []
    cursor = 0
    for index, item in enumerate(ordered_inputs):
        samples, input_rate = sf.read(item.blob_path, dtype="float64", always_2d=True)
        mono = np.mean(np.asarray(samples), axis=1)
        normalized = _resample(mono, input_rate, sample_rate)
        start_seconds = cursor / sample_rate
        parts.append(normalized)
        cursor += normalized.size
        end_seconds = cursor / sample_rate
        pause_after_ms = item.pause_after_ms if index + 1 < len(ordered_inputs) else 0
        entries.append(
            ChapterTimelineSegment(
                ordinal=item.ordinal,
                segment_id=item.segment_id,
                gsv_version_id=item.gsv_version_id,
                gsv_content_sha256=item.gsv_content_sha256,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                pause_after_ms=pause_after_ms,
            )
        )
        if pause_after_ms:
            silence = np.zeros(round(sample_rate * pause_after_ms / 1000), dtype=np.float64)
            parts.append(silence)
            cursor += silence.size
    return (
        np.concatenate(parts),
        ChapterTimeline(segments=tuple(entries), duration_seconds=cursor / sample_rate),
    )


def _sample_rate_of(path: Path) -> int:
    return int(sf.info(path).samplerate)


def _resample(samples: np.ndarray, input_rate: int, output_rate: int) -> np.ndarray:
    if input_rate == output_rate:
        return samples
    output_frames = round(samples.size * output_rate / input_rate)
    source_x = np.arange(samples.size, dtype=np.float64)
    target_x = np.linspace(0, samples.size - 1, output_frames, dtype=np.float64)
    return np.asarray(np.interp(target_x, source_x, samples), dtype=np.float64)


def _write_json(path: Path, payload: object) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _rollback_if_active(reservation: OutputReservation) -> None:
    try:
        reservation.rollback()
    except PipelineError:
        pass
