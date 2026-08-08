from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from voice_pipeline.models.model_profiles import ImportModelProfileRequest


def test_import_request_requires_a_ckpt_and_a_pth(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=".ckpt"):
        ImportModelProfileRequest(
            display_name="voice-v1",
            gpt_source_path=(tmp_path / "voice.bin").resolve(),
            sovits_source_path=(tmp_path / "voice.pth").resolve(),
        )


def test_import_request_requires_absolute_source_paths(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ImportModelProfileRequest(
            display_name="voice-v1",
            gpt_source_path=Path("voice.ckpt"),
            sovits_source_path=(tmp_path / "voice.pth").resolve(),
        )


def test_gsv_request_accepts_an_optional_model_profile_id(tmp_path: Path) -> None:
    from voice_pipeline.models.schemas import GsvSynthesisRequest, ReferenceBinding
    from tests.unit.test_schemas import REQUEST_ID, _binding_payload

    request = GsvSynthesisRequest(
        request_id=REQUEST_ID,
        reference=ReferenceBinding.model_validate(_binding_payload(tmp_path)),
        text="This is a test.",
        text_lang="en",
        model_profile_id=uuid4(),
    )

    assert request.model_profile_id is not None
