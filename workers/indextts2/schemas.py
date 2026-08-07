from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from voice_pipeline.models.schemas import EmotionVector, NonBlankText


class WorkerSynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    text: NonBlankText
    speaker_audio_path: Path
    emotion_vector: EmotionVector
    seed: int
    use_random: Literal[False] = False
    output_path: Path
