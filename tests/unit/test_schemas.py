from __future__ import annotations

import pytest
from pydantic import ValidationError

from voice_pipeline.models.schemas import (
    AudioResult,
    GsvSynthesisRequest,
    IndexSynthesisRequest,
    ReferenceBinding,
    ReferenceJobRequest,
    SegmentSynthesisRequest,
)

VALID = [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20]

INVALID_VECTORS = [
    [0.1] * 7,
    [0.1] * 9,
    [-0.1, 0, 0, 0, 0, 0, 0, 0],
    [1.1, 0, 0, 0, 0, 0, 0, 0],
    [0.11] * 8,
]

REQUEST_ID = "d613a571-1d69-4f6e-a1b7-3222f61657b8"
REF_TEXT = "我已经失去了一切，可我仍然活着。"


@pytest.mark.parametrize("vector", INVALID_VECTORS)
def test_rejects_invalid_emotion_vectors(vector: list[float], tmp_path) -> None:
    with pytest.raises(ValidationError):
        IndexSynthesisRequest(
            request_id=REQUEST_ID,
            text=REF_TEXT,
            speaker_audio_path=(tmp_path / "voice.wav").resolve(),
            emotion_vector=vector,
            seed=1234,
        )


def test_preserves_vector_exactly(tmp_path) -> None:
    request = IndexSynthesisRequest(
        request_id=REQUEST_ID,
        text=REF_TEXT,
        speaker_audio_path=(tmp_path / "voice.wav").resolve(),
        emotion_vector=VALID,
        seed=1234,
    )
    assert list(request.emotion_vector) == VALID


@pytest.mark.parametrize("vector", INVALID_VECTORS)
def test_segment_request_rejects_invalid_vectors(vector: list[float], tmp_path) -> None:
    with pytest.raises(ValidationError):
        SegmentSynthesisRequest(
            request_id=REQUEST_ID,
            base_voice_path=(tmp_path / "voice.wav").resolve(),
            ref_text_cn=REF_TEXT,
            emotion_vector=vector,
            target_text="私はまだ生きている。",
            target_language="ja",
            seed=1234,
        )


@pytest.mark.parametrize("vector", INVALID_VECTORS)
def test_reference_job_request_rejects_invalid_vectors(vector: list[float], tmp_path) -> None:
    with pytest.raises(ValidationError):
        ReferenceJobRequest(
            request_id=REQUEST_ID,
            base_voice_path=(tmp_path / "voice.wav").resolve(),
            ref_text_cn=REF_TEXT,
            emotion_vector=vector,
            seed=1234,
        )


@pytest.mark.parametrize("invalid_reference", ["これは参考です。", "English reference only."])
def test_reference_job_rejects_non_chinese_reference_text(
    invalid_reference: str, tmp_path
) -> None:
    with pytest.raises(ValidationError):
        ReferenceJobRequest(
            request_id=REQUEST_ID,
            base_voice_path=(tmp_path / "voice.wav").resolve(),
            ref_text_cn=invalid_reference,
            emotion_vector=VALID,
            seed=1234,
        )


def _binding_payload(tmp_path) -> dict[str, object]:
    return {
        "audio": {
            "path": str((tmp_path / "reference.wav").resolve()),
            "duration_seconds": 4.0,
            "sample_rate": 22050,
            "channels": 1,
            "frames": 88200,
            "content_sha256": "0" * 64,
            "rms_dbfs": -17.0,
            "peak_dbfs": -14.0,
        },
        "ref_text_cn": REF_TEXT,
        "emotion_vector": VALID,
        "base_voice_sha256": "1" * 64,
        "engine_fingerprint": {
            "schema_version": 1,
            "engine": "indextts",
            "source_revision": "90ca4d608209584bad3a5bd5becc0b80c146e60f",
            "model_revision": "740dcaff396282ffb241903d150ac011cd4b1ede",
            "engine_lock_sha256": "2" * 64,
            "checkpoint_lock_sha256": "3" * 64,
            "environment_lock_sha256": "4" * 64,
            "runtime_config_sha256": "5" * 64,
        },
    }


@pytest.mark.parametrize("vector", INVALID_VECTORS)
def test_reference_binding_rejects_invalid_vectors_from_json(vector: list[float], tmp_path) -> None:
    payload = _binding_payload(tmp_path)
    payload["emotion_vector"] = vector
    with pytest.raises(ValidationError):
        ReferenceBinding.model_validate(payload)


BLANK_TEXTS = ["", "   ", "\r\n\t", "\t\n "]


@pytest.mark.parametrize("blank", BLANK_TEXTS)
def test_index_text_rejects_blank(blank: str, tmp_path) -> None:
    with pytest.raises(ValidationError):
        IndexSynthesisRequest(
            request_id=REQUEST_ID,
            text=blank,
            speaker_audio_path=(tmp_path / "voice.wav").resolve(),
            emotion_vector=VALID,
            seed=1234,
        )


@pytest.mark.parametrize("blank", BLANK_TEXTS)
def test_reference_binding_rejects_blank_ref_text(blank: str, tmp_path) -> None:
    payload = _binding_payload(tmp_path)
    payload["ref_text_cn"] = blank
    with pytest.raises(ValidationError):
        ReferenceBinding.model_validate(payload)


@pytest.mark.parametrize("blank", BLANK_TEXTS)
def test_segment_request_rejects_blank_texts(blank: str, tmp_path) -> None:
    with pytest.raises(ValidationError):
        SegmentSynthesisRequest(
            request_id=REQUEST_ID,
            base_voice_path=(tmp_path / "voice.wav").resolve(),
            ref_text_cn=blank,
            emotion_vector=VALID,
            target_text="私はまだ生きている。",
            target_language="ja",
            seed=1234,
        )
    with pytest.raises(ValidationError):
        SegmentSynthesisRequest(
            request_id=REQUEST_ID,
            base_voice_path=(tmp_path / "voice.wav").resolve(),
            ref_text_cn=REF_TEXT,
            emotion_vector=VALID,
            target_text=blank,
            target_language="ja",
            seed=1234,
        )


@pytest.mark.parametrize("blank", BLANK_TEXTS)
def test_gsv_request_rejects_blank_text(blank: str, tmp_path) -> None:
    binding = ReferenceBinding.model_validate(_binding_payload(tmp_path))
    with pytest.raises(ValidationError):
        GsvSynthesisRequest(
            request_id=REQUEST_ID,
            reference=binding,
            text=blank,
            text_lang="ja",
        )


def test_non_blank_text_strips_value(tmp_path) -> None:
    request = IndexSynthesisRequest(
        request_id=REQUEST_ID,
        text=f"  {REF_TEXT}  ",
        speaker_audio_path=(tmp_path / "voice.wav").resolve(),
        emotion_vector=VALID,
        seed=1234,
    )
    assert request.text == REF_TEXT


def test_audio_result_requires_valid_content_sha(tmp_path) -> None:
    with pytest.raises(ValidationError):
        AudioResult(
            path=tmp_path / "a.wav",
            duration_seconds=4.0,
            sample_rate=22050,
            channels=1,
            frames=88200,
            content_sha256="not-hex",
            rms_dbfs=-17.0,
            peak_dbfs=-14.0,
        )
