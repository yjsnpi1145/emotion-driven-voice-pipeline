from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import httpx
import pytest
import respx

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director_llm import (
    EmotionContextUnit,
    EmotionDirectionInput,
    PreprocessRewriteUnit,
    TranslationInput,
)
from voice_pipeline.modules.llm.activity import LlmActivityLog
from voice_pipeline.modules.llm.client import OpenAiDirectorClient
from voice_pipeline.modules.llm.script_chunking import build_analysis_units, split_script


@pytest.mark.asyncio
@respx.mock
async def test_preprocess_rewrite_uses_strict_unit_payload_and_prompt() -> None:
    units = (
        PreprocessRewriteUnit(
            unit_id="unit-1",
            text="“Your Majesty，欠款168万。”",
            context="quoted_dialogue",
        ),
    )
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "items": [
                                    {
                                        "unit_id": "unit-1",
                                        "rewritten_text": "“Your Majesty，欠款168万。”",
                                        "input_unit_ids": ["unit-1"],
                                    }
                                ]
                            }
                        }
                    }
                ]
            },
        )
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )
    activity = LlmActivityLog()
    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(
            settings,
            http_client=http,
            api_key="secret",
            activity=activity,
        )
        result = await client.rewrite_preprocess_paragraph(
            paragraph_id="paragraph-1",
            units=units,
        )

    assert result.items[0].unit_id == "unit-1"
    body = json.loads(route.calls[0].request.content)
    prompt = body["messages"][0]["content"]
    payload = json.loads(body["messages"][1]["content"])
    assert payload == {
        "paragraph_id": "paragraph-1",
        "units": [
            {
                "unit_id": "unit-1",
                "text": "“Your Majesty，欠款168万。”",
                "context": "quoted_dialogue",
            }
        ],
    }
    assert "Never add or delete plot information" in prompt
    assert "Never translate" in prompt
    events = (await activity.snapshot()).events
    assert [event.operation for event in events] == ["script_preprocessing"] * 2
    assert [event.kind for event in events] == ["request_sent", "response"]


def _analysis_response(unit_ids: list[str]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "content": {
                        "units": [
                            {
                                "unit_id": unit_id,
                                "kind": "narration" if index == 0 else "dialogue",
                                "temporary_role_name": None if index == 0 else "甲",
                                "role_aliases": [],
                                "role_confidence": 0.9,
                                "speak_enabled": True,
                            }
                            for index, unit_id in enumerate(unit_ids)
                        ]
                    }
                }
            }
        ]
    }


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
                                "synthesis_text": "これは目標言語の配音本文です。",
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
    activity = LlmActivityLog()
    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, activity=activity)
        plan = await client.create_plan(source_text=source, target_language="ja")

    activity_payload = (await activity.snapshot()).model_dump(mode="json")

    assert route.called
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {secret}"
    request_body = json.loads(route.calls[0].request.content)
    system_prompt = request_body["messages"][0]["content"]
    user_payload = json.loads(request_body["messages"][1]["content"])
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert user_payload["source_text_sha256"] == source_sha256
    assert user_payload["source_length"] == len(source)
    assert source_sha256 in system_prompt
    for required_field in (
        "ordinal",
        "source_start",
        "source_end",
        "emotion_description",
        "emotion_vector",
        "synthesis_text",
        "ref_text_cn",
        "pause_after_ms",
        "speed_factor",
        "seed",
    ):
        assert required_field in system_prompt
    assert "<= 0.8" in system_prompt
    assert "synthesis_text must be written in target_language" in system_prompt
    assert "translate the complete source slice" in system_prompt
    assert "ref_text_cn must always be natural Simplified Chinese" in system_prompt
    assert "never copy Japanese, English, Korean" in system_prompt
    assert plan.segments[0].source_end == 2
    assert plan.segments[0].synthesis_text == "これは目標言語の配音本文です。"
    assert secret not in repr(plan)
    assert secret not in os.environ.get("PIPELINE_LLM_KEY", "")[: -len(secret)]
    assert [event["kind"] for event in activity_payload["events"]] == [
        "request_sent",
        "response",
    ]
    assert "synthesis_text" in activity_payload["events"][-1]["content"]
    serialized_activity = json.dumps(activity_payload, ensure_ascii=False)
    assert secret not in serialized_activity
    assert "authorization" not in serialized_activity.casefold()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("segment_update", "expected_path"),
    [
        ({"synthesis_text": None}, "segments.0.synthesis_text"),
        ({"ref_text_cn": "これは日本語の参考文です。"}, "segments.0.ref_text_cn"),
    ],
)
async def test_openai_client_rejects_missing_translation_or_non_chinese_reference(
    segment_update: dict[str, object], expected_path: str
) -> None:
    source = "これは原文です。"
    segment: dict[str, object] = {
        "ordinal": 0,
        "source_start": 0,
        "source_end": len(source),
        "emotion_description": "平静",
        "emotion_vector": [0, 0, 0, 0, 0, 0, 0, 0.3],
        "synthesis_text": source,
        "ref_text_cn": "这是一段平静的中文参考。",
        "pause_after_ms": 0,
        "speed_factor": 1.0,
        "seed": 1234,
    }
    segment.update(segment_update)
    respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "source_text_sha256": hashlib.sha256(
                                    source.encode("utf-8")
                                ).hexdigest(),
                                "segments": [segment],
                            }
                        }
                    }
                ]
            },
        )
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )
    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, api_key="secret")
        with pytest.raises(PipelineError) as exc_info:
            await client.create_plan(source_text=source, target_language="ja")

    assert exc_info.value.code == ErrorCode.LLM_INVALID_RESPONSE
    assert exc_info.value.details["schema_errors"][0]["path"] == expected_path


@pytest.mark.asyncio
@respx.mock
async def test_script_analysis_requests_classifications_and_materializes_local_slices() -> None:
    source = "旁白。\n甲：你好。"
    chunk = split_script(source, max_chars=100)[0]
    units = build_analysis_units(chunk)
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_analysis_response([unit.unit_id for unit in units]))
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, api_key="secret")
        result = await client.analyze_script_chunk(chunk=chunk)

    request_body = json.loads(route.calls[0].request.content)
    system_prompt = request_body["messages"][0]["content"]
    user_payload = json.loads(request_body["messages"][1]["content"])
    assert set(user_payload) == {"chunk_id", "units"}
    assert all(
        set(unit) == {"unit_id", "source_text", "context"}
        for unit in user_payload["units"]
    )
    assert [unit["context"] for unit in user_payload["units"]] == [
        unit.context for unit in units
    ]
    assert "quoted_dialogue" in system_prompt
    assert "quote_bridge_narration" in system_prompt
    assert "source_start" not in system_prompt
    assert "source_end" not in system_prompt
    assert [item.source_text for item in result.utterances] == [unit.source_text for unit in units]
    assert [item.source_start for item in result.utterances] == [
        unit.source_start for unit in units
    ]


@pytest.mark.asyncio
@respx.mock
async def test_script_analysis_repairs_one_invalid_unit_id_response() -> None:
    chunk = split_script("旁白。\n甲：你好。", max_chars=100)[0]
    units = build_analysis_units(chunk)
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=_analysis_response([units[0].unit_id])),
            httpx.Response(
                200,
                json=_analysis_response([unit.unit_id for unit in units]),
            ),
        ]
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )
    activity = LlmActivityLog()

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(
            settings, http_client=http, api_key="secret", activity=activity
        )
        result = await client.analyze_script_chunk(chunk=chunk)

    assert route.call_count == 2
    assert "".join(item.source_text for item in result.utterances) == chunk.source_text
    events = (await activity.snapshot()).model_dump(mode="json")["events"]
    assert [item["kind"] for item in events] == [
        "request_sent",
        "response",
        "retrying",
        "request_sent",
        "response",
    ]
    assert "结构化修复" in events[2]["message"]


@pytest.mark.asyncio
@respx.mock
async def test_script_analysis_rejects_a_second_invalid_unit_id_response() -> None:
    chunk = split_script("旁白。\n甲：你好。", max_chars=100)[0]
    units = build_analysis_units(chunk)
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=_analysis_response([units[0].unit_id])),
            httpx.Response(200, json=_analysis_response(["unknown:u9999"])),
        ]
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, api_key="secret")
        with pytest.raises(PipelineError) as exc:
            await client.analyze_script_chunk(chunk=chunk)

    assert route.call_count == 2
    assert exc.value.code == ErrorCode.LLM_INVALID_RESPONSE
    assert "unit IDs" in exc.value.message


@pytest.mark.asyncio
@respx.mock
async def test_translation_prompt_expands_only_chinese_reference_for_short_dialogue() -> None:
    utterance_id = uuid4()
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "items": [
                                    {
                                        "utterance_id": str(utterance_id),
                                        "revision": 3,
                                        "synthesis_text": "え？",
                                        "ref_text_cn": "咦？我刚才似乎听见了什么声音。",
                                        "emotion_vector": [0, 0, 0, 0, 0, 0, 0.2, 0.1],
                                        "speed_factor": 1.0,
                                        "pause_after_ms": 400,
                                    }
                                ]
                            }
                        }
                    }
                ]
            },
        )
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, api_key="secret")
        await client.translate_utterances(
            target_language="ja",
            utterances=(
                TranslationInput(
                    utterance_id=utterance_id,
                    revision=3,
                    source_text="诶？",
                ),
            ),
        )

    prompt = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert "3 to 10 seconds" in prompt
    assert "expand ref_text_cn" in prompt
    assert "do not change synthesis_text" in prompt
    assert "emotion_vector" in prompt


@pytest.mark.asyncio
@respx.mock
async def test_emotion_direction_sends_scene_speaker_and_timeline_context() -> None:
    utterance_id = uuid4()
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "items": [
                                    {
                                        "utterance_id": str(utterance_id),
                                        "revision": 3,
                                        "emotion_vector": [0, 0, 0.35, 0, 0, 0.2, 0, 0.1],
                                    }
                                ]
                            }
                        }
                    }
                ]
            },
        )
    )
    context = EmotionDirectionInput(
        utterance_id=utterance_id,
        revision=3,
        role_name="甲",
        source_text="嗯。",
        scene_context="她收到噩耗，却强忍泪水。甲：\u201c我没事。\u201d乙：\u201c真的吗？\u201d甲：\u201c嗯。\u201d",
        previous_units=(
            EmotionContextUnit(
                ordinal=1,
                role_name="乙",
                kind="dialogue",
                speak_enabled=True,
                text="真的吗？",
            ),
        ),
        next_units=(),
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )
    activity = LlmActivityLog()

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(
            settings,
            http_client=http,
            api_key="secret",
            activity=activity,
        )
        result = await client.direct_emotions(utterances=(context,))

    request = json.loads(route.calls[0].request.content)
    prompt = request["messages"][0]["content"]
    payload = json.loads(request["messages"][1]["content"])
    assert payload == {"utterances": [context.model_dump(mode="json")]}
    assert "Do not judge an interjection in isolation" in prompt
    assert "scene_context" in prompt
    assert "speaker" in prompt
    assert "Never output a uniform vector" in prompt
    assert result.items[0].utterance_id == utterance_id
    events = (await activity.snapshot()).events
    assert [event.operation for event in events] == ["emotion_direction"] * 2


@pytest.mark.asyncio
@respx.mock
async def test_reference_correction_prompt_preserves_emotion_and_changes_only_length() -> None:
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "ref_text_cn": "咦？我刚才似乎听见了什么声音。"
                            }
                        }
                    }
                ]
            },
        )
    )
    settings = LlmSettings(
        mode="openai",
        base_url="https://llm.example/v1",
        model="director",
        api_key_env="PIPELINE_LLM_KEY",
    )

    async with httpx.AsyncClient(base_url=settings.base_url + "/") as http:
        client = OpenAiDirectorClient(settings, http_client=http, api_key="secret")
        await client.correct_reference_text(
            current="诶？",
            direction="lengthen",
            emotion_description="保持当前情绪向量和表演强度",
        )

    prompt = json.loads(route.calls[0].request.content)["messages"][0]["content"]
    assert "Simplified Chinese" in prompt
    assert "only its spoken duration" in prompt
    assert "preserve" in prompt.casefold()
    assert "3.0..10.0" in prompt
