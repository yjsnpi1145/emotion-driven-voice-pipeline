from __future__ import annotations

from io import BytesIO

import numpy as np
import soundfile as sf


def append_trailing_silence(source_wav: bytes, *, silence_ms: int) -> bytes:
    """Render a standalone WAV copy with its configured trailing pause."""
    if not 0 <= silence_ms <= 30_000:
        raise ValueError("trailing silence must be within 0..30000 milliseconds")
    if silence_ms == 0:
        return source_wav

    source = BytesIO(source_wav)
    info = sf.info(source)
    source.seek(0)
    samples, sample_rate = sf.read(source, dtype="float64", always_2d=True)
    silence_frames = round(sample_rate * silence_ms / 1000)
    silence = np.zeros((silence_frames, samples.shape[1]), dtype=np.float64)

    rendered = BytesIO()
    sf.write(
        rendered,
        np.concatenate((samples, silence), axis=0),
        sample_rate,
        format="WAV",
        subtype=info.subtype,
    )
    return rendered.getvalue()
