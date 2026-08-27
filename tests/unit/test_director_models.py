from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from voice_pipeline.models.director import (
    CreateDirectorProjectRequest,
    CreateRolePresetRequest,
    DirectorUtterancePatch,
    DirectorUtteranceRecord,
)


def valid_utterance() -> dict[str, object]:
    return {
        "utterance_id": uuid4(),
        "project_id": uuid4(),
        "ordinal": 0,
        "source_start": 0,
        "source_end": 2,
        "source_text": "你好",
        "working_text": "你好",
        "kind": "dialogue",
        "speak_enabled": True,
        "role_id": uuid4(),
        "role_confidence": 0.95,
        "role_confirmed": True,
        "synthesis_text": None,
        "ref_text_cn": None,
        "emotion_vector": None,
        "speed_factor": 1.0,
        "pause_after_ms": 0,
        "seed": 1234,
        "revision": 0,
    }


def test_director_utterance_requires_exact_forward_range() -> None:
    with pytest.raises(ValidationError):
        DirectorUtteranceRecord.model_validate(
            {**valid_utterance(), "source_start": 5, "source_end": 5}
        )


def test_role_preset_rejects_non_wav_managed_path() -> None:
    with pytest.raises(ValidationError):
        CreateRolePresetRequest(
            name="林雪",
            base_voice_path=Path("voice.mp3"),
            model_profile_id=uuid4(),
            default_speed=1.0,
        )


def test_project_preserves_source_whitespace() -> None:
    request = CreateDirectorProjectRequest(
        title="场景一",
        source_text=" 旁白\n角色：你好。 ",
        source_language="auto",
        target_language="ja",
    )
    assert request.source_text == " 旁白\n角色：你好。 "


def test_patch_rejects_invalid_emotion_sum() -> None:
    with pytest.raises(ValidationError):
        DirectorUtterancePatch(expected_revision=0, emotion_vector=[0.2] * 8)


def test_patch_preserves_non_blank_working_text_whitespace() -> None:
    patch = DirectorUtterancePatch(expected_revision=0, working_text="  修改后的台词。 \n")

    assert patch.working_text == "  修改后的台词。 \n"


def test_patch_rejects_blank_working_text() -> None:
    with pytest.raises(ValidationError):
        DirectorUtterancePatch(expected_revision=0, working_text=" \r\n\t ")
