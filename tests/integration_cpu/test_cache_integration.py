from __future__ import annotations

import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app
from voice_pipeline.modules.gpt_sovits.fake import FakeGptSoVitsClient
from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient
from voice_pipeline.modules.quality.fake import DeterministicQualityAnalyzer


async def _wait(client: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(200):
        payload = (await client.get(f"/api/v1/jobs/{job_id}")).json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_identical_segment_uses_reference_and_gsv_caches(fake_settings, request_json) -> None:
    class CountingAnalyzer(DeterministicQualityAnalyzer):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def analyze_reference(self, *, audio_path, expected_text):
            self.calls += 1
            return await super().analyze_reference(
                audio_path=audio_path,
                expected_text=expected_text,
            )

    index = FakeIndexTTSClient()
    gsv = FakeGptSoVitsClient()
    analyzer = CountingAnalyzer()
    app = create_app(fake_settings, index_client=index, gsv_client=gsv)
    app.state.plane.service.configure_quality(analyzer)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/api/v1/jobs/segment", json=request_json)
            assert (await _wait(client, first.json()["job_id"]))["status"] == "succeeded"
            assert (index.calls, gsv.calls) == (1, 1)

            second = await client.post("/api/v1/jobs/segment", json=request_json)
            second_status = await _wait(client, second.json()["job_id"])

    assert second_status["status"] == "succeeded"
    assert (index.calls, gsv.calls) == (1, 1)
    assert analyzer.calls == 1
