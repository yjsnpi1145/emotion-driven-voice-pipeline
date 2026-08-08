from __future__ import annotations

import http.server
import threading
from uuid import uuid4

import httpx
import pytest

from voice_pipeline.api.dependencies import fingerprint_from_challenge
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.runtime.external import ExternalEngineRuntime


class _BoomServer:
    """Minimal server that 500s /health/ready and /__control/abort."""

    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._reply(500, b'{"error":"boom"}')

            def do_POST(self) -> None:
                self._reply(500, b'{"error":"boom"}')

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _runtime(external_settings, *, wrong: bool = False) -> ExternalEngineRuntime:
    if wrong:
        fingerprints = {
            "indextts": fingerprint_from_challenge("indextts", "wrong-challenge"),
            "gpt_sovits": fingerprint_from_challenge("gpt_sovits", "wrong-challenge"),
        }
    else:
        fingerprints = {
            "indextts": fingerprint_from_challenge("indextts", "external-test-index"),
            "gpt_sovits": fingerprint_from_challenge("gpt_sovits", "external-test-gsv"),
        }
    return ExternalEngineRuntime(settings=external_settings, fingerprints=fingerprints)


@pytest.mark.asyncio
async def test_ensure_engine_accepts_matching_fingerprint(
    external_settings, external_servers
) -> None:
    runtime = _runtime(external_settings)
    await runtime.ensure_engine("indextts")
    await runtime.ensure_engine("gpt_sovits")
    identity = runtime.engine_identity("indextts")
    assert identity.worker == "indextts"
    assert runtime.health().status == "ready"


@pytest.mark.asyncio
async def test_ensure_engine_rejects_fingerprint_mismatch(
    external_settings, external_servers
) -> None:
    runtime = _runtime(external_settings, wrong=True)
    with pytest.raises(PipelineError) as exc_info:
        await runtime.ensure_engine("indextts")
    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_ensure_engine_http_failure(external_settings, external_servers) -> None:
    boom = _BoomServer()
    try:
        runtime = _runtime(external_settings)
        runtime._base_url = lambda engine: boom.base_url  # type: ignore[method-assign]
        with pytest.raises(PipelineError) as exc_info:
            await runtime.ensure_engine("indextts")
        assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
        assert exc_info.value.retryable is True
    finally:
        boom.close()


@pytest.mark.asyncio
async def test_engine_identity_raises_when_not_ready(external_settings, external_servers) -> None:
    runtime = _runtime(external_settings)
    with pytest.raises(PipelineError) as exc_info:
        runtime.engine_identity("indextts")
    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE


@pytest.mark.asyncio
async def test_abort_engine_success(external_settings, external_servers) -> None:
    runtime = _runtime(external_settings)
    await runtime.ensure_engine("indextts")
    await runtime.abort_engine("indextts", reason="test")
    with pytest.raises(PipelineError):
        runtime.engine_identity("indextts")


@pytest.mark.asyncio
async def test_abort_engine_unconfirmed_poisons_queue(external_settings, external_servers) -> None:
    boom = _BoomServer()
    try:
        runtime = _runtime(external_settings)
        runtime._base_url = lambda engine: boom.base_url  # type: ignore[method-assign]
        with pytest.raises(PipelineError) as exc_info:
            await runtime.abort_engine("indextts", reason="test")
        assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
        assert exc_info.value.poison_queue is True
    finally:
        boom.close()


@pytest.mark.asyncio
async def test_mark_unknown_degrades_health(external_settings, external_servers) -> None:
    runtime = _runtime(external_settings)
    await runtime.ensure_engine("indextts")
    lease = await runtime.begin_inference("indextts", job_id=uuid4())
    await lease.mark_unknown()
    health = runtime.health()
    assert health.workers.indextts.state == "unknown"
    assert health.status == "degraded"


@pytest.mark.asyncio
async def test_unknown_engine_raises(external_settings, external_servers) -> None:
    runtime = _runtime(external_settings)
    with pytest.raises(ValueError, match="unknown engine"):
        runtime.engine_identity("bogus")
    with pytest.raises(ValueError, match="unknown engine"):
        await runtime.begin_inference("bogus", job_id=uuid4())


@pytest.mark.asyncio
async def test_external_fake_engine_reports_activity_status(external_servers) -> None:
    """The black-box fixture exposes activity without importing product runtime state."""
    async with httpx.AsyncClient(base_url=external_servers[0].base_url, timeout=5) as client:
        response = await client.get("/__control/status")

    assert response.status_code == 200
    assert response.json() == {"active_inference": 0, "max_active_observed": 0}
