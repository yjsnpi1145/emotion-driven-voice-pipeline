from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from uuid import uuid4

from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.schemas import EmotionVector, ExecutionContext, ReferenceJobRequest


class ServiceReferenceDurationProbe:
    """Measure an IndexTTS reference without enforcing the final GSV time window."""

    def __init__(
        self,
        *,
        synthesis: SynthesisService,
        queue: SerialGpuQueue,
        jobs_root: Path,
        base_voice: Path,
    ) -> None:
        self._synthesis = synthesis
        self._queue = queue
        self._jobs_root = jobs_root
        self._base_voice = base_voice

    async def generate_and_measure(
        self,
        text: str,
        vector: EmotionVector,
        seed: int,
    ) -> float:
        request_id = uuid4()
        job_id = uuid4()
        context = ExecutionContext(
            job_id=job_id,
            request_id=request_id,
            job_dir=self._jobs_root / str(job_id),
        )
        request = ReferenceJobRequest(
            request_id=request_id,
            base_voice_path=self._base_voice,
            ref_text_cn=text,
            emotion_vector=vector,
            seed=seed,
        )
        try:
            result = await self._queue.run(
                lambda: self._synthesis.generate_reference(
                    context,
                    request,
                    enforce_reference_window=False,
                )
            )
            return result.reference.audio.duration_seconds
        finally:
            await asyncio.to_thread(shutil.rmtree, context.job_dir, True)
