from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.conftest import (
    RecordingAuditWriter,
    RecordingEngineRuntime,
    RecordingGsvClient,
    RecordingIndexClient,
    make_context,
    make_request,
    write_tone,
)
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.schemas import ExecutionContext, SegmentSynthesisRequest
from voice_pipeline.modules.quality.fake import DeterministicQualityAnalyzer
from voice_pipeline.modules.quality.models import QualityPolicy
from voice_pipeline.modules.quality.text import evaluate_quality


@pytest.mark.asyncio
async def test_segment_runs_index_then_gsv_with_bound_prompt(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    audit_events: list[dict[str, object]] = []
    index = RecordingIndexClient(calls, duration_seconds=4.0)
    gsv = RecordingGsvClient(calls)
    runtime = RecordingEngineRuntime(calls)
    service = SynthesisService(
        index=index,
        gsv=gsv,
        runtime=runtime,
        audit=RecordingAuditWriter(audit_events),
    )
    request = SegmentSynthesisRequest(
        request_id="cf2deece-f4e8-4114-954b-bfc907730e01",
        base_voice_path=(tmp_path / "音色 voice.wav").resolve(),
        ref_text_cn="我已经失去了一切，可我仍然活着。",
        emotion_vector=[0, 0.02, 0.28, 0.03, 0, 0.27, 0, 0.20],
        target_text="私はすべてを失った。それでも、まだ生きている。",
        target_language="ja",
        seed=1234,
    )
    write_tone(request.base_voice_path, seconds=5.0)
    context = ExecutionContext(
        job_id="aaaaaaaa-0000-4000-8000-000000000001",
        request_id=request.request_id,
        job_dir=tmp_path / "jobs" / "aaaaaaaa-0000-4000-8000-000000000001",
    )

    result = await service.synthesize_segment(context, request)

    assert [name for name, _ in calls] == [
        "ensure:indextts",
        "index",
        "ensure:gpt_sovits",
        "gsv",
    ]
    gsv_request = calls[3][1]
    assert gsv_request.reference.ref_text_cn == request.ref_text_cn
    assert gsv_request.reference.audio.path == result.reference.path
    assert result.job_id == context.job_id
    assert result.reference.path.parent == context.job_dir
    assert [event["engine"] for event in audit_events if event["event"] == "inference_started"] == [
        "indextts",
        "gpt_sovits",
    ]


@pytest.mark.asyncio
async def test_invalid_reference_never_calls_gsv(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    audit_events: list[dict[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=2.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter(audit_events),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    with pytest.raises(Exception, match="REFERENCE_DURATION_OUT_OF_RANGE"):
        await service.synthesize_segment(context, request)
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]


@pytest.mark.asyncio
async def test_reference_job_accepts_minute_long_base_voice_and_only_calls_index(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    audit_events: list[dict[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter(audit_events),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=60.0)
    context = make_context(tmp_path, request.request_id)

    result = await service.generate_reference(context, request)

    assert [name for name, _ in calls] == ["ensure:indextts", "index"]
    assert result.manifest_path.exists()
    assert result.reference.ref_text_cn == request.ref_text_cn


@pytest.mark.asyncio
async def test_reference_duration_probe_can_measure_audio_outside_final_window(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=2.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    service.configure_quality(DeterministicQualityAnalyzer())
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)

    result = await service.generate_reference(
        context,
        request,
        enforce_reference_window=False,
    )

    assert result.reference.audio.duration_seconds == pytest.approx(2.0, abs=0.01)
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]


@pytest.mark.asyncio
async def test_quality_failure_keeps_index_diagnostic_and_never_calls_gsv(tmp_path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    class RejectingAnalyzer:
        @property
        def policy_fingerprint(self) -> str:
            return QualityPolicy().fingerprint()

        async def analyze_reference(self, *, audio_path: Path, expected_text: str):
            return evaluate_quality(
                total_seconds=4.0,
                speech_seconds=0.0,
                expected_text=expected_text,
                transcript="",
                policy=QualityPolicy(),
            )

    calls: list[tuple[str, object]] = []
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    service.configure_quality(RejectingAnalyzer())

    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)

    assert exc_info.value.code == ErrorCode.QUALITY_VAD_FAILED
    assert (context.job_dir / "reference.wav").is_file()
    assert not (context.job_dir / "reference-manifest.json").exists()
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]


@pytest.mark.asyncio
async def test_reference_manifest_contains_quality_result(tmp_path) -> None:
    import json

    calls: list[tuple[str, object]] = []
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    service.configure_quality(DeterministicQualityAnalyzer())

    result = await service.generate_reference(context, request)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["quality_result"]["passed"] is True
    assert manifest["quality_result"]["policy_fingerprint"] == (
        DeterministicQualityAnalyzer().policy_fingerprint
    )


@pytest.mark.asyncio
async def test_mismatched_request_id_is_rejected_before_file_creation(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, "11111111-2222-4333-8444-555555555555")
    with pytest.raises(Exception, match="INVALID_INPUT"):
        await service.synthesize_segment(context, request)
    assert not context.job_dir.exists()
    assert calls == []


@pytest.mark.asyncio
async def test_missing_base_voice_is_rejected_before_job_dir(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    context = make_context(tmp_path, request.request_id)
    with pytest.raises(Exception, match="INVALID_INPUT"):
        await service.synthesize_segment(context, request)
    assert not context.job_dir.exists()
    assert calls == []


@pytest.mark.asyncio
async def test_relative_base_voice_path_is_rejected(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    request = request.model_copy(update={"base_voice_path": Path("relative/voice.wav")})
    context = make_context(tmp_path, request.request_id)
    with pytest.raises(Exception, match="INVALID_INPUT"):
        await service.synthesize_segment(context, request)
    assert not context.job_dir.exists()


@pytest.mark.asyncio
async def test_same_request_id_twice_creates_distinct_job_dirs(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context1 = make_context(tmp_path, request.request_id)
    context2 = make_context(tmp_path, request.request_id)

    result1 = await service.synthesize_segment(context1, request)
    result2 = await service.synthesize_segment(context2, request)

    assert context1.job_dir != context2.job_dir
    assert result1.job_id != result2.job_id
    assert result1.reference.path.parent == context1.job_dir
    assert result2.reference.path.parent == context2.job_dir


@pytest.mark.asyncio
async def test_gsv_job_rejects_manifest_sha_mismatch(tmp_path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError
    from voice_pipeline.models.schemas import GsvJobRequest

    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    ref_result = await service.generate_reference(context, request)

    # Tamper with the reference wav so its sha no longer matches the manifest.
    # Offset 44 is the PCM data start for a 16-bit mono header; write at a
    # non-zero sample position so the bytes actually change.
    import asyncio

    def _tamper() -> None:
        with open(ref_result.reference.audio.path, "r+b") as fh:
            fh.seek(44 + 22050)
            fh.write(b"\xff\x7f")

    await asyncio.to_thread(_tamper)

    gsv_request = GsvJobRequest(
        request_id="22222222-3333-4444-8555-666666666666",
        reference_manifest_path=ref_result.manifest_path,
        target_text="私はまだ生きている。",
        target_language="ja",
        seed=1234,
    )
    gsv_context = make_context(tmp_path, gsv_request.request_id)
    with pytest.raises(PipelineError) as exc_info:
        await service.generate_gsv(gsv_context, gsv_request)
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]


@pytest.mark.asyncio
async def test_gsv_job_uses_manifest_binding(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    ref_result = await service.generate_reference(context, request)

    gsv_request = make_gsv_request(ref_result.manifest_path)
    gsv_context = make_context(tmp_path, gsv_request.request_id)
    result = await service.generate_gsv(gsv_context, gsv_request)

    assert [name for name, _ in calls] == ["ensure:indextts", "index", "ensure:gpt_sovits", "gsv"]
    gsv_call = calls[3][1]
    assert gsv_call.reference.ref_text_cn == ref_result.reference.ref_text_cn
    assert result.reference_content_sha256 == ref_result.reference.audio.content_sha256


@pytest.mark.asyncio
async def test_gsv_quality_enabled_service_rejects_legacy_manifest_without_report(tmp_path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    calls: list[tuple[str, object]] = []
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    reference_service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    ref_result = await reference_service.generate_reference(
        make_context(tmp_path, request.request_id), request
    )
    gsv_service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter([]),
    )
    gsv_service.configure_quality(DeterministicQualityAnalyzer())
    gsv_request = make_gsv_request(ref_result.manifest_path)

    with pytest.raises(PipelineError) as exc_info:
        await gsv_service.generate_gsv(make_context(tmp_path, gsv_request.request_id), gsv_request)

    assert exc_info.value.code == ErrorCode.QUALITY_TEXT_MISMATCH
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]


def make_gsv_request(manifest_path):
    from voice_pipeline.models.schemas import GsvJobRequest

    return GsvJobRequest(
        request_id="22222222-3333-4444-8555-666666666666",
        reference_manifest_path=manifest_path,
        target_text="私はまだ生きている。",
        target_language="ja",
        seed=1234,
    )
