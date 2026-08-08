from __future__ import annotations

import hashlib
import os

import httpx
import pytest
import respx

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.modules.llm.client import OpenAiDirectorClient


@pytest.mark.asyncio
@respx.mock
async def test_openai_client_uses_chat_completions_without_exposing_secret(monkeypatch) -> None:
    secret = "do-not-persist-this-secret"
    monkeypatch.setenv("PIPELINE_LLM_KEY", secret)
    source = "甲乙"
    response = {
        "choices": [
            {
                "message": {
                    "content": {
                        "source_text_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        "segments": [
                            {
                                "ordinal": 0,
                                "source_start": 0,
                                "source_end": 2,
                                "emotion_description": "平静",
                                "emotion_vector": [0, 0, 0, 0, 0, 0, 0, 0.3],
                                "ref_text_cn": "我很平静。",
                                "pause_after_ms": 0,
                                "speed_factor": 1.0,
                                "seed": 1234,
                            }
                        ],
                    }
                }
            }
        ]
    }
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=response)
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )
    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http)
        plan = await client.create_plan(source_text=source, target_language="ja")

    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {secret}"
    assert plan.segments[0].source_end == 2
    assert secret not in repr(plan)
    assert secret not in os.environ.get("PIPELINE_LLM_KEY", "")[: -len(secret)]
