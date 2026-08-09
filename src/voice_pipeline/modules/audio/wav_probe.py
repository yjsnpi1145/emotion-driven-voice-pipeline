from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
import soundfile as sf

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import AudioResult


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_wav(path: Path, *, require_reference_window: bool) -> AudioResult:
    try:
        data, sample_rate = sf.read(path, dtype="float64")
    except Exception as exc:
        raise PipelineError(
            ErrorCode.INVALID_AUDIO,
            "audio",
            f"could not decode wav: {path.name}",
            retryable=False,
        ) from exc

    if data.ndim == 1:
        channels = 1
        samples = data
    else:
        channels = data.shape[1]
        samples = data[:, 0]

    if channels != 1:
        raise PipelineError(
            ErrorCode.INVALID_AUDIO,
            "audio",
            f"wav must be mono but has {channels} channels",
            retryable=False,
        )

    if not np.all(np.isfinite(samples)):
        raise PipelineError(
            ErrorCode.INVALID_AUDIO,
            "audio",
            "wav contains non-finite samples",
            retryable=False,
        )

    duration = float(samples.shape[0]) / float(sample_rate)
    if require_reference_window:
        if not (3.0 <= duration <= 10.0):
            raise PipelineError(
                ErrorCode.REFERENCE_DURATION_OUT_OF_RANGE,
                "audio",
                f"reference duration {duration:.3f}s outside closed 3.0..10.0",
                retryable=False,
            )
    elif duration <= 0.1:
        raise PipelineError(
            ErrorCode.INVALID_AUDIO,
            "audio",
            f"target duration {duration:.3f}s too short",
            retryable=False,
        )

    rms = float(np.sqrt(np.mean(np.square(samples))))
    rms_dbfs = 20.0 * math.log10(max(rms, 1e-12))
    peak = float(np.max(np.abs(samples)))
    peak_dbfs = 20.0 * math.log10(max(peak, 1e-12))
    if rms_dbfs <= -50.0:
        raise PipelineError(
            ErrorCode.AUDIO_SILENT,
            "audio",
            "wav is silent",
            retryable=False,
        )

    return AudioResult(
        path=path,
        duration_seconds=duration,
        sample_rate=int(sample_rate),
        channels=1,
        frames=int(samples.shape[0]),
        content_sha256=sha256_file(path),
        rms_dbfs=rms_dbfs,
        peak_dbfs=peak_dbfs,
    )
