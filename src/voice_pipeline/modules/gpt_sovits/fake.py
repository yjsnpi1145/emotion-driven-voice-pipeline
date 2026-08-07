from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.core.inference_tracker import fake_fingerprint
from voice_pipeline.models.schemas import (
    AudioResult,
    EngineFingerprint,
    GsvSynthesisRequest,
)
from voice_pipeline.modules.audio.atomic_output import reserve_output_path
from voice_pipeline.modules.audio.wav_probe import probe_wav


class FakeGptSoVitsClient:
    """In-process deterministic GSV fake producing a valid 1.5s mono WAV."""

    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        failure: PipelineError | None = None,
    ) -> None:
        self._delay_seconds = delay_seconds
        self._failure = failure

    def fingerprint(self) -> EngineFingerprint:
        return fake_fingerprint("gpt_sovits")

    async def synthesize(self, request: GsvSynthesisRequest, output_path: Path) -> AudioResult:
        if self._failure is not None:
            raise self._failure
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        reservation = reserve_output_path(output_path)
        partial = output_path.with_name(f".{output_path.stem}.{uuid.uuid4()}.partial.wav")
        try:
            payload = request.model_dump(mode="json")
            from voice_pipeline.modules.indextts.fake import frequency_for

            freq = frequency_for(payload, base=220)
            sample_rate = 32000
            duration = 1.5
            t = np.arange(int(duration * sample_rate)) / sample_rate
            data = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
            sf.write(partial, data, sample_rate, subtype="PCM_16")
            probed = probe_wav(partial, require_reference_window=False)
            reservation.publish(partial)
            return probed.model_copy(update={"path": reservation.path})
        except BaseException:
            reservation.rollback()
            raise
        finally:
            partial.unlink(missing_ok=True)
