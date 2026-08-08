from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from voice_pipeline.models.chapter import ChapterSynthesisRequest


def test_chapter_request_rejects_blank_source_text() -> None:
    with pytest.raises(ValidationError, match="text must not be blank"):
        ChapterSynthesisRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "title": "chapter",
                "source_text": "   ",
                "target_language": "ja",
                "base_voice_path": "C:/voices/base.wav",
                "model_profile_id": str(uuid4()),
            }
        )


def test_chapter_request_requires_model_profile_id() -> None:
    with pytest.raises(ValidationError, match="model_profile_id"):
        ChapterSynthesisRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "title": "chapter",
                "source_text": "正文",
                "target_language": "ja",
                "base_voice_path": "C:/voices/base.wav",
            }
        )
