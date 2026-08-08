from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import Field

from voice_pipeline.models.persistence import JsonValue, OutputAudioSpec
from voice_pipeline.models.schemas import (
    EngineFingerprint,
    GsvSynthesisRequest,
    IndexSynthesisRequest,
    StrictModel,
)


class CanonicalCacheKey(StrictModel):
    kind: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_payload_json: str
    payload: dict[str, JsonValue]


def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def canonical_key(kind: str, payload: Mapping[str, JsonValue]) -> CanonicalCacheKey:
    envelope: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": kind,
        "payload": dict(payload),
    }
    serialized = canonical_json(envelope)
    return CanonicalCacheKey(
        kind=kind,
        sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        canonical_payload_json=serialized,
        payload=dict(payload),
    )


def build_reference_cache_key(
    request: IndexSynthesisRequest,
    *,
    base_voice_sha256: str,
    engine_fingerprint: EngineFingerprint,
    output_spec: OutputAudioSpec,
) -> CanonicalCacheKey:
    return canonical_key(
        "reference",
        {
            "text": request.text,
            "base_voice_sha256": base_voice_sha256,
            "emotion_vector": list(request.emotion_vector),
            "seed": request.seed,
            "use_random": request.use_random,
            "engine_fingerprint": engine_fingerprint.model_dump(mode="json"),
            "output_spec": output_spec.model_dump(mode="json"),
        },
    )


def build_gsv_cache_key(
    request: GsvSynthesisRequest,
    *,
    engine_fingerprint: EngineFingerprint,
    output_spec: OutputAudioSpec,
) -> CanonicalCacheKey:
    return canonical_key(
        "gsv",
        {
            "reference_audio_sha256": request.reference.audio.content_sha256,
            "prompt_text": request.reference.ref_text_cn,
            "prompt_lang": request.prompt_lang,
            "text": request.text,
            "text_lang": request.text_lang,
            "speed_factor": request.speed_factor,
            "seed": request.seed,
            "model_profile_id": str(request.model_profile_id) if request.model_profile_id else None,
            "engine_fingerprint": engine_fingerprint.model_dump(mode="json"),
            "output_spec": output_spec.model_dump(mode="json"),
        },
    )


def build_quality_cache_key(
    *, audio_sha256: str, expected_text: str, policy_fingerprint: str
) -> CanonicalCacheKey:
    expected_text_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    return canonical_key(
        "reference_quality",
        {
            "audio_sha256": audio_sha256,
            "expected_text_sha256": expected_text_sha256,
            "policy_fingerprint": policy_fingerprint,
        },
    )
