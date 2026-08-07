from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.contract.conftest import EXPECTED_INDEX_FINGERPRINT, write_valid_reference
from tests.unit.conftest import (
    RecordingAuditWriter,
    RecordingEngineRuntime,
    RecordingGsvClient,
    make_context,
    make_request,
    write_tone,
)
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.modules.indextts.client import IndexTTSHttpClient


def _request_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_index_client_sends_exact_vector_and_absolute_paths(index_request, tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_request_body(request))
        worker_output = Path(str(seen["output_path"]))
        write_valid_reference(worker_output, seconds=4.0)
        return httpx.Response(
            200,
            json={
                "request_id": str(index_request.request_id),
                "output_path": str(worker_output.resolve()),
                "effective_emotion_vector": list(index_request.emotion_vector),
                "engine_fingerprint": EXPECTED_INDEX_FINGERPRINT.model_dump(mode="json"),
            },
        )

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=5,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    await client.synthesize(index_request, tmp_path / "reference.wav")

    assert seen["emotion_vector"] == list(index_request.emotion_vector)
    assert seen["use_random"] is False
    assert seen["speaker_audio_path"] == str(index_request.speaker_audio_path)


def _make_service(index_client, tmp_path):
    calls: list[tuple[str, object]] = []
    runtime = RecordingEngineRuntime(calls)
    service = SynthesisService(
        index=index_client,
        gsv=RecordingGsvClient(calls),
        runtime=runtime,
        audit=RecordingAuditWriter([]),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    return service, request, context, calls


def _aborted(calls) -> bool:
    return any(name == "abort:indextts" for name, _ in calls)


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json={})


@pytest.mark.asyncio
async def test_read_timeout_triggers_abort(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=1,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_TIMEOUT
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_post_dispatch_reset_triggers_abort(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("connection reset", request=request)

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=1,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_truncated_stream_triggers_abort(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_body(request)
        worker_output = Path(str(body["output_path"]))
        write_valid_reference(worker_output, seconds=4.0)
        # Malformed JSON body -> truncated stream equivalent
        return httpx.Response(200, content=b"{not-json")

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=1,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
    assert exc_info.value.requires_engine_abort
    assert _aborted(calls)


@pytest.mark.asyncio
async def test_cancellation_triggers_abort(index_request, tmp_path) -> None:
    transport = _BlockingTransport()
    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=30,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
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
async def test_connect_before_dispatch_does_not_abort(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=1,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort
    assert not _aborted(calls)


@pytest.mark.asyncio
async def test_complete_http_500_does_not_abort(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=1,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort
    assert not _aborted(calls)


@pytest.mark.asyncio
async def test_fingerprint_mismatch_is_rejected(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_body(request)
        worker_output = Path(str(body["output_path"]))
        write_valid_reference(worker_output, seconds=4.0)
        from tests.contract.conftest import EXPECTED_GSV_FINGERPRINT

        return httpx.Response(
            200,
            json={
                "request_id": str(index_request.request_id),
                "output_path": str(worker_output.resolve()),
                "effective_emotion_vector": list(index_request.emotion_vector),
                "engine_fingerprint": EXPECTED_GSV_FINGERPRINT.model_dump(mode="json"),
            },
        )

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=5,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
    assert not exc_info.value.requires_engine_abort


@pytest.mark.asyncio
async def test_effective_vector_mismatch_is_rejected(index_request, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _request_body(request)
        worker_output = Path(str(body["output_path"]))
        write_valid_reference(worker_output, seconds=4.0)
        return httpx.Response(
            200,
            json={
                "request_id": str(index_request.request_id),
                "output_path": str(worker_output.resolve()),
                "effective_emotion_vector": [0.9, 0, 0, 0, 0, 0, 0, 0],
                "engine_fingerprint": EXPECTED_INDEX_FINGERPRINT.model_dump(mode="json"),
            },
        )

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=5,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    service, request, context, calls = _make_service(client, tmp_path)
    with pytest.raises(PipelineError) as exc_info:
        await service.synthesize_segment(context, request)
    assert exc_info.value.code == ErrorCode.INDEX_ENGINE_ERROR
