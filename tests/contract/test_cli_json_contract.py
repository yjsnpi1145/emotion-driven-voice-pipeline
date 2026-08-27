from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from voice_pipeline.api.app import create_app
from voice_pipeline.cli import app as cli_app
from voice_pipeline.modules.audio.wav_probe import sha256_file

runner = CliRunner()


@pytest.fixture
async def server_url(fake_settings, tmp_path: Path) -> Any:
    app = create_app(fake_settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(60):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.05)
    except TimeoutError:
        pytest.fail("uvicorn did not start within 60 seconds")
    port = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()


@pytest.fixture
def request_json(tmp_path: Path) -> dict[str, object]:
    from tests.integration_cpu.conftest import write_tone

    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    return {
        "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
        "base_voice_path": str(base_voice.resolve()),
        "ref_text_cn": "我已经失去了一切，可我仍然活着。",
        "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        "target_text": "私はまだ生きている。",
        "target_language": "ja",
        "seed": 1234,
    }


@pytest.mark.asyncio
async def test_synthesize_segment_cli_end_to_end(server_url, request_json, tmp_path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request_json), encoding="utf-8")
    out_dir = tmp_path / "out"

    result = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "synthesize-segment",
            "--server",
            server_url,
            "--request",
            str(request_file),
            "--output-dir",
            str(out_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    for name in (
        "reference.wav",
        "reference-manifest.json",
        "target.wav",
        "run-manifest.json",
    ):
        assert (out_dir / name).exists(), name
    # independent decode
    import soundfile as sf

    data, sr = sf.read(out_dir / "target.wav")
    assert data.size > 0 and sr > 0


@pytest.mark.asyncio
async def test_synthesize_segment_refuses_preexisting_output(
    server_url, request_json, tmp_path
) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(request_json), encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sentinel = out_dir / "target.wav"
    sentinel.write_bytes(b"sentinel-bytes")
    before = sha256_file(sentinel)

    result = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "synthesize-segment",
            "--server",
            server_url,
            "--request",
            str(request_file),
            "--output-dir",
            str(out_dir),
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "OUTPUT_CONFLICT"
    assert sha256_file(sentinel) == before


@pytest.mark.asyncio
async def test_generate_reference_then_gsv_leaves_reference_unchanged(
    server_url, request_json, tmp_path
) -> None:
    ref_payload = {
        key: value
        for key, value in request_json.items()
        if key not in ("target_text", "target_language")
    }
    ref_request = tmp_path / "ref-request.json"
    ref_request.write_text(json.dumps(ref_payload), encoding="utf-8")
    ref_out = tmp_path / "ref.wav"

    result = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "generate-reference",
            "--server",
            server_url,
            "--request",
            str(ref_request),
            "--output",
            str(ref_out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    manifest_path = tmp_path / "ref.reference-manifest.json"
    assert ref_out.exists()
    assert manifest_path.exists()

    ref_sha_before = sha256_file(ref_out)
    manifest_sha_before = sha256_file(manifest_path)

    gsv_payload = {
        "request_id": "4de7ed6a-00f0-4be6-b916-1f10cf96019e",
        "reference_manifest_path": str(manifest_path.resolve()),
        "target_text": "私はまだ生きている。",
        "target_language": "ja",
        "seed": 1234,
    }
    gsv_request = tmp_path / "gsv-request.json"
    gsv_request.write_text(json.dumps(gsv_payload), encoding="utf-8")
    gsv_out = tmp_path / "target.wav"

    result = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "generate-gsv",
            "--server",
            server_url,
            "--request",
            str(gsv_request),
            "--output",
            str(gsv_out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert gsv_out.exists()
    # generate-gsv must not modify the reference wav or its manifest
    assert sha256_file(ref_out) == ref_sha_before
    assert sha256_file(manifest_path) == manifest_sha_before


@pytest.mark.asyncio
async def test_maintenance_retention_cli_uses_the_loopback_http_api(server_url) -> None:
    planned = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "maintenance",
            "retention-plan",
            "--server",
            server_url,
            "--json",
        ],
    )
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.stdout)
    assert plan["candidate_version_ids"] == []

    applied = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        [
            "maintenance",
            "retention-apply",
            plan["plan_id"],
            "--server",
            server_url,
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout) == {
        "plan_id": plan["plan_id"],
        "status": "applied",
        "deleted_version_ids": [],
    }

    cache_status = await asyncio.to_thread(
        runner.invoke,
        cli_app,
        ["maintenance", "cache-status", "--server", server_url, "--json"],
    )
    assert cache_status.exit_code == 0, cache_status.output
    assert json.loads(cache_status.stdout) == {"entries": []}
