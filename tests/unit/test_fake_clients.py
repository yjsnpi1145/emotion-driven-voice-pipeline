from __future__ import annotations

import time

import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.audio.wav_probe import probe_wav
from voice_pipeline.modules.gpt_sovits.fake import FakeGptSoVitsClient
from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient, frequency_for


def test_frequency_is_deterministic_from_payload() -> None:
    payload = {"text": "你好", "seed": 1, "列表": [1, 2]}
    assert frequency_for(payload, base=100) == frequency_for(payload, base=100)
    assert frequency_for({"text": "你好", "seed": 1}, base=100) == frequency_for(
        {"text": "你好", "seed": 1}, base=100
    )


def test_frequency_differs_between_payloads() -> None:
    a = frequency_for({"text": "你好"}, base=100)
    b = frequency_for({"text": "你好啊"}, base=100)
    assert a != b


@pytest.mark.asyncio
async def test_index_fake_writes_valid_reference(tmp_path, index_request) -> None:
    client = FakeIndexTTSClient()
    output = tmp_path / "reference.wav"
    result = await client.synthesize(index_request, output)
    assert result.path == output.resolve()
    assert 3.0 <= result.duration_seconds <= 9.0
    assert result.channels == 1
    probed = probe_wav(output, require_reference_window=True)
    assert probed.content_sha256 == result.content_sha256


@pytest.mark.asyncio
async def test_gsv_fake_writes_valid_target(tmp_path, gsv_request) -> None:
    client = FakeGptSoVitsClient()
    output = tmp_path / "target.wav"
    result = await client.synthesize(gsv_request, output)
    assert result.path == output.resolve()
    assert result.duration_seconds > 0.5
    assert result.channels == 1


@pytest.mark.asyncio
async def test_index_fake_respects_delay(tmp_path, index_request) -> None:
    client = FakeIndexTTSClient(delay_seconds=0.05)
    started = time.monotonic()
    await client.synthesize(index_request, tmp_path / "r.wav")
    assert time.monotonic() - started >= 0.04


@pytest.mark.asyncio
async def test_gsv_fake_respects_delay(tmp_path, gsv_request) -> None:
    client = FakeGptSoVitsClient(delay_seconds=0.05)
    started = time.monotonic()
    await client.synthesize(gsv_request, tmp_path / "t.wav")
    assert time.monotonic() - started >= 0.04


@pytest.mark.asyncio
async def test_index_fake_injects_failure_and_rolls_back(tmp_path, index_request) -> None:
    failure = PipelineError(ErrorCode.INDEX_ENGINE_ERROR, "index", "boom", retryable=False)
    client = FakeIndexTTSClient(failure=failure)
    output = tmp_path / "r.wav"
    with pytest.raises(PipelineError, match="boom"):
        await client.synthesize(index_request, output)
    assert not output.exists()


@pytest.mark.asyncio
async def test_gsv_fake_injects_failure_and_rolls_back(tmp_path, gsv_request) -> None:
    failure = PipelineError(ErrorCode.GSV_ENGINE_ERROR, "gsv", "boom", retryable=False)
    client = FakeGptSoVitsClient(failure=failure)
    output = tmp_path / "t.wav"
    with pytest.raises(PipelineError, match="boom"):
        await client.synthesize(gsv_request, output)
    assert not output.exists()


@pytest.mark.asyncio
async def test_index_fake_rejects_existing_target(tmp_path, index_request) -> None:
    client = FakeIndexTTSClient()
    output = tmp_path / "r.wav"
    output.write_bytes(b"sentinel")
    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        await client.synthesize(index_request, output)
    assert output.read_bytes() == b"sentinel"


@pytest.mark.asyncio
async def test_fake_fingerprints_are_filled_and_distinct() -> None:
    index_fp = FakeIndexTTSClient().fingerprint()
    gsv_fp = FakeGptSoVitsClient().fingerprint()
    assert index_fp.engine == "indextts"
    assert gsv_fp.engine == "gpt_sovits"
    assert index_fp.source_revision == "in-process-fake"
    assert len(index_fp.engine_lock_sha256) == 64
    assert index_fp.engine_lock_sha256 != gsv_fp.engine_lock_sha256
