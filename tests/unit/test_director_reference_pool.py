from __future__ import annotations

from voice_pipeline.core.director_reference_pool import (
    build_pool_family_key,
    effective_reference_text,
    is_short_reference,
    reference_spec,
    select_emotion_bucket,
)


def test_short_reference_counts_unicode_content_not_punctuation() -> None:
    assert effective_reference_text(' “嗯？” ') == "嗯"
    assert effective_reference_text("「砰！」") == "砰"
    assert is_short_reference(' “嗯？” ')
    assert is_short_reference("「砰！」")
    assert not is_short_reference('“嗯，Your Majesty”')
    assert not is_short_reference("为什么")
    assert not is_short_reference("……")


def test_emotion_bucket_requires_strength_and_margin() -> None:
    assert select_emotion_bucket((0.20, 0.01, 0, 0, 0, 0, 0, 0)) == "joy"
    assert select_emotion_bucket((0.14, 0, 0, 0, 0, 0, 0, 0)) == "calm"
    assert select_emotion_bucket((0.20, 0.16, 0, 0, 0, 0, 0, 0)) == "calm"
    assert select_emotion_bucket((0.10,) * 8) == "calm"
    assert select_emotion_bucket((0, 0, 0, 0, 0, 0, 0, 0.20)) == "calm"


def test_reference_spec_uses_exact_template_vector_and_new_revision_seed() -> None:
    first = reference_spec("surprise", revision=0, attempt=0)
    rebuilt = reference_spec("surprise", revision=1, attempt=0)

    assert first.prompt_text == "我完全没有想到，事情竟然会变成这样。"
    assert first.emotion_vector == (0, 0, 0, 0, 0, 0, 0.6, 0.2)
    assert first.seed == 1234
    assert rebuilt.seed != first.seed
    assert reference_spec("calm", revision=0, attempt=2).emotion_vector == (
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0.8,
    )


def test_pool_family_key_is_stable_and_excludes_retry_seed() -> None:
    common = {
        "base_voice_sha256": "a" * 64,
        "bucket": "surprise",
        "engine_fingerprint": {"engine": "indextts", "model_revision": "model-a"},
        "output_spec": {"sample_rate": 22050, "channels": 1},
    }

    assert build_pool_family_key(**common) == build_pool_family_key(**common)
    assert build_pool_family_key(**common) != build_pool_family_key(
        **{**common, "base_voice_sha256": "b" * 64}
    )
