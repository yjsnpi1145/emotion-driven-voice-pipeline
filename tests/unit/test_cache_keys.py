from __future__ import annotations

from voice_pipeline.models.persistence import OutputAudioSpec
from voice_pipeline.modules.cache.keys import build_gsv_cache_key, build_reference_cache_key


def test_reference_key_preserves_emotion_values_without_rescaling(gsv_request) -> None:
    from voice_pipeline.core.inference_tracker import fake_fingerprint
    from voice_pipeline.models.schemas import IndexSynthesisRequest

    vector = (0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20)
    request = IndexSynthesisRequest(
        request_id=gsv_request.request_id,
        text="我仍然活着。",
        speaker_audio_path=gsv_request.reference.audio.path,
        emotion_vector=vector,
        seed=1234,
    )
    key = build_reference_cache_key(
        request,
        base_voice_sha256="a" * 64,
        engine_fingerprint=fake_fingerprint("indextts"),
        output_spec=OutputAudioSpec(sample_rate=22050),
    )
    changed = build_reference_cache_key(
        request.model_copy(update={"seed": 1235}),
        base_voice_sha256="a" * 64,
        engine_fingerprint=fake_fingerprint("indextts"),
        output_spec=OutputAudioSpec(sample_rate=22050),
    )
    assert key.payload["emotion_vector"] == list(vector)
    assert key.sha256 != changed.sha256


def test_gsv_key_changes_when_reference_or_speed_changes(gsv_request) -> None:
    from voice_pipeline.core.inference_tracker import fake_fingerprint

    baseline = build_gsv_cache_key(
        gsv_request,
        engine_fingerprint=fake_fingerprint("gpt_sovits"),
        output_spec=OutputAudioSpec(sample_rate=32000),
    )
    changed = build_gsv_cache_key(
        gsv_request.model_copy(update={"speed_factor": 1.1}),
        engine_fingerprint=fake_fingerprint("gpt_sovits"),
        output_spec=OutputAudioSpec(sample_rate=32000),
    )
    assert baseline.sha256 != changed.sha256
