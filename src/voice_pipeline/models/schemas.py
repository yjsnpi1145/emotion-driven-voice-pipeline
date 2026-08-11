from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

RawEmotionVector = tuple[float, float, float, float, float, float, float, float]
LanguageCode = Literal["zh", "ja", "en", "ko", "yue"]
WorkerName = Literal["indextts", "gpt_sovits"]


def _validate_emotion_vector(value: RawEmotionVector) -> RawEmotionVector:
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in value):
        raise ValueError("emotion vector values must be finite and within 0.0..1.0")
    if math.fsum(value) > 0.8 + 1e-9:
        raise ValueError("emotion vector sum must be <= 0.8")
    return value


EmotionVector = Annotated[RawEmotionVector, AfterValidator(_validate_emotion_vector)]


def _validate_non_blank_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("text must not be blank")
    return stripped


NonBlankText = Annotated[str, AfterValidator(_validate_non_blank_text)]


def _validate_preserved_non_blank_text(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


PreservedNonBlankText = Annotated[str, AfterValidator(_validate_preserved_non_blank_text)]


def _validate_chinese_reference_text(value: str) -> str:
    stripped = _validate_non_blank_text(value)
    if not any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\uf900" <= character <= "\ufaff"
        for character in stripped
    ):
        raise ValueError("Chinese reference text must contain a CJK ideograph")
    if any(
        "\u3040" <= character <= "\u30ff" or "\uff66" <= character <= "\uff9d"
        for character in stripped
    ):
        raise ValueError("Chinese reference text must not contain Japanese kana")
    return stripped


ChineseReferenceText = Annotated[str, AfterValidator(_validate_chinese_reference_text)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionContext(StrictModel):
    job_id: UUID
    request_id: UUID
    job_dir: Path


class AudioResult(StrictModel):
    path: Path
    duration_seconds: float = Field(gt=0)
    sample_rate: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=2)
    frames: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rms_dbfs: float
    peak_dbfs: float


class EngineFingerprint(StrictModel):
    schema_version: Literal[1]
    engine: WorkerName
    source_revision: str
    model_revision: str
    engine_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EngineIdentity(StrictModel):
    worker: WorkerName
    pid: int
    create_time: float
    python_executable: Path
    fingerprint: EngineFingerprint


class WorkerHealth(StrictModel):
    state: Literal["ready", "stopped_expected", "starting", "unhealthy", "unknown"]
    pid: int | None
    create_time: float | None
    python_executable: Path
    python_version: str
    source_revision: str
    fingerprint: EngineFingerprint
    preflight_ok: bool
    active_inference: int = Field(default=0, ge=0, le=1)


class WorkersHealth(StrictModel):
    indextts: WorkerHealth
    gpt_sovits: WorkerHealth


class RuntimeHealth(StrictModel):
    status: Literal["ready", "degraded", "stopping", "stopped"]
    workers: WorkersHealth


class IndexSynthesisRequest(StrictModel):
    request_id: UUID
    text: ChineseReferenceText
    speaker_audio_path: Path
    emotion_vector: EmotionVector
    seed: int
    use_random: Literal[False] = False


class ReferenceBinding(StrictModel):
    audio: AudioResult
    ref_text_cn: ChineseReferenceText
    emotion_vector: EmotionVector
    base_voice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_fingerprint: EngineFingerprint


class GsvSynthesisRequest(StrictModel):
    request_id: UUID
    reference: ReferenceBinding
    text: NonBlankText
    text_lang: LanguageCode
    prompt_lang: Literal["zh"] = "zh"
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: int = -1
    model_profile_id: UUID | None = None


class SegmentSynthesisRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    ref_text_cn: ChineseReferenceText
    emotion_vector: EmotionVector
    target_text: NonBlankText
    target_language: LanguageCode
    seed: int = 1234
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    model_profile_id: UUID | None = None


class ReferenceJobRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    ref_text_cn: ChineseReferenceText
    emotion_vector: EmotionVector
    seed: int = 1234


class GsvJobRequest(StrictModel):
    request_id: UUID
    reference_manifest_path: Path
    target_text: NonBlankText
    target_language: LanguageCode
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: int = 1234
    model_profile_id: UUID | None = None


class ReferenceSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    reference: ReferenceBinding
    manifest_path: Path


class GsvSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    target: AudioResult
    reference_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_path: Path


class SegmentSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    reference: AudioResult
    target: AudioResult
    reference_binding: ReferenceBinding
    reference_manifest_path: Path
    run_manifest_path: Path
