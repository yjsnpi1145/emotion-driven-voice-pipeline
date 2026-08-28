from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from voice_pipeline.models.schemas import EmotionVector

EmotionBucket = Literal[
    "joy",
    "anger",
    "sadness",
    "fear",
    "disgust",
    "melancholy",
    "surprise",
    "calm",
]

EMOTION_BUCKETS: tuple[EmotionBucket, ...] = (
    "joy",
    "anger",
    "sadness",
    "fear",
    "disgust",
    "melancholy",
    "surprise",
    "calm",
)
POOL_TEMPLATE_VERSION = 1
POOL_RETRY_SEEDS = (1234, 2345, 3456)

POOL_TEMPLATES: dict[EmotionBucket, str] = {
    "calm": "我明白了，接下来就按照原定计划继续吧。",
    "joy": "听到这个消息，我心里真的感到非常高兴。",
    "anger": "这件事情让我无法继续保持冷静。",
    "sadness": "想到已经发生的一切，我心里还是很难过。",
    "fear": "周围突然安静下来，让我感到有些害怕。",
    "disgust": "这样的做法，实在让人感到难以接受。",
    "melancholy": "回想起从前的事情，心里难免有些惆怅。",
    "surprise": "我完全没有想到，事情竟然会变成这样。",
}


@dataclass(frozen=True, slots=True)
class PoolReferenceSpec:
    bucket: EmotionBucket
    template_version: int
    prompt_text: str
    emotion_vector: EmotionVector
    revision: int
    attempt: int
    seed: int


def effective_reference_text(text: str) -> str:
    """Return only Unicode letters and numbers used for short-reference routing."""

    return "".join(
        character
        for character in text
        if unicodedata.category(character)[:1] in {"L", "N"}
    )


def is_short_reference(text: str) -> bool:
    length = len(effective_reference_text(text))
    return 1 <= length <= 2


def select_emotion_bucket(vector: Sequence[float]) -> EmotionBucket:
    if len(vector) != 8:
        raise ValueError("emotion vector must contain exactly eight values")
    values = tuple(float(value) for value in vector)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("emotion vector values must be finite")
    maximum = max(values)
    maximum_indexes = [index for index, value in enumerate(values) if math.isclose(value, maximum)]
    calm_index = 7
    if calm_index in maximum_indexes or len(maximum_indexes) != 1:
        return "calm"
    winner = maximum_indexes[0]
    second = max(value for index, value in enumerate(values) if index != winner)
    if maximum + 1e-9 < 0.15 or maximum - second + 1e-9 < 0.05:
        return "calm"
    return EMOTION_BUCKETS[winner]


def reference_spec(
    bucket: EmotionBucket, *, revision: int, attempt: int
) -> PoolReferenceSpec:
    if revision < 0:
        raise ValueError("pool revision must be non-negative")
    if attempt < 0 or attempt >= len(POOL_RETRY_SEEDS):
        raise ValueError("pool attempt must be within 0..2")
    vector = [0.0] * 8
    if bucket == "calm":
        vector[7] = 0.8
    else:
        vector[EMOTION_BUCKETS.index(bucket)] = 0.6
        vector[7] = 0.2
    return PoolReferenceSpec(
        bucket=bucket,
        template_version=POOL_TEMPLATE_VERSION,
        prompt_text=POOL_TEMPLATES[bucket],
        emotion_vector=cast(EmotionVector, tuple(vector)),
        revision=revision,
        attempt=attempt,
        seed=POOL_RETRY_SEEDS[attempt] + revision * 100_003,
    )


def build_pool_family_key(
    *,
    base_voice_sha256: str,
    bucket: EmotionBucket,
    engine_fingerprint: Mapping[str, Any] | Any,
    output_spec: Mapping[str, Any] | Any,
) -> str:
    spec = reference_spec(bucket, revision=0, attempt=0)
    payload = {
        "schema_version": 1,
        "base_voice_sha256": base_voice_sha256,
        "bucket": bucket,
        "template_version": spec.template_version,
        "prompt_text": spec.prompt_text,
        "emotion_vector": list(spec.emotion_vector),
        "engine_fingerprint": _dump(engine_fingerprint),
        "output_spec": _dump(output_spec),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dump(value: Mapping[str, Any] | Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("pool key inputs must be mappings or Pydantic models")
