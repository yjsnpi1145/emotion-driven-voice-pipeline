from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from tests.contract.conftest import (
    EXPECTED_GSV_FINGERPRINT,
    valid_wav_bytes,
)
from tests.unit.conftest import (
    RecordingAuditWriter,
    RecordingEngineRuntime,
    RecordingIndexClient,
    make_context,
    make_request,
    write_tone,
)
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.model_profiles import ResolvedModelProfile
from voice_pipeline.modules.gpt_sovits.client import GptSoVitsHttpClient

EXPECTED_PAYLOAD = {
    "text": "私はまだ生きている。",
    "text_lang": "ja",
    "ref_audio_path": "",
    "prompt_text": "我已经失去了一切，可我仍然活着。",
    "prompt_lang": "zh",
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0,
    "text_split_method": "cut0",
    "batch_size": 1,
    "split_bucket": False,
    "speed_factor": 1.0,
    "fragment_interval": 0.0,
    "seed": 1234,
    "parallel_infer": False,
    "repetition_penalty": 1.35,
    "media_type": "wav",
    "streaming_mode": False,
}


def _request_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_load_profile_calls_official_weight_endpoints_before_tts(
    gsv_request, tmp_path
) -> None:
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(request.url.path)
        if request.url.path in ("/set_gpt_weights", "/set_sovits_weights"):
            return httpx.Response(200, json={"message": "success"})
        if request.url.path == "/tts":
            return httpx.Response(200, content=valid_wav_bytes(seconds=1.5))
        return httpx.Response(404)

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=5,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    profile = ResolvedModelProfile(
        profile_id=uuid4(),
        display_name="voice-v1",
        gpt_relative_path="profiles/a/GPT/model.ckpt",
        sovits_relative_path="profiles/a/SoVITS/model.pth",
        gpt_sha256="a" * 64,
        sovits_sha256="b" * 64,
        gpt_path=(tmp_path / "model.ckpt").resolve(),
        sovits_path=(tmp_path / "model.pth").resolve(),
    )

    await client.load_profile(profile)
    await client.synthesize(gsv_request, tmp_path / "target.wav")

    assert events == ["/set_gpt_weights", "/set_sovits_weights", "/tts"]


@pytest.mark.asyncio
async def test_gsv_payload_uses_bound_reference_text(gsv_request, tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        return httpx.Response(200, content=valid_wav_bytes(seconds=1.5))

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=5,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    await client.synthesize(gsv_request, tmp_path / "target.wav")

    expected = dict(EXPECTED_PAYLOAD)
    expected["ref_audio_path"] = str(gsv_request.reference.audio.path)
    assert seen == expected


def _make_service(client, tmp_path):
    calls: list[tuple[str, object]] = []
    runtime = RecordingEngineRuntime(calls)
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=4.0),
        gsv=client,
        runtime=runtime,
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    return service, request, context, calls


def _aborted(calls) -> bool:
    return any(name == "abort:gpt_sovits" for name, _ in calls)


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, content=valid_wav_bytes(seconds=1.5))


@pytest.mark.asyncio
async def test_gsv_read_timeout_triggers_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_TIMEOUT
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_post_dispatch_reset_triggers_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("reset", request=request)

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_ENGINE_ERROR
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_truncated_stream_triggers_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Truncated WAV stream: fewer bytes than the header promises
        body = valid_wav_bytes(seconds=1.5)
        return httpx.Response(200, content=body[:40])

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_ENGINE_ERROR
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_cancellation_triggers_abort(gsv_request, tmp_path) -> None:
    transport = _BlockingTransport()
    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=30,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=transport,
    )
    service, request, context, calls = _make_service(client, tmp_path)
    task = asyncio.create_task(service.synthesize_segment(context, request))
    await transport.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_connect_before_dispatch_does_not_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort
    assert not _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_complete_http_500_does_not_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort
    assert not _aborted(calls)


@pytest.mark.asyncio
async def test_gsv_json_content_type_rejected_without_abort(gsv_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "disguised"})

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=1,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.GSV_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort
    assert not _aborted(calls)
