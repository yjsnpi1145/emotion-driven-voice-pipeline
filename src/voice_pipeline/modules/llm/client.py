from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.models import (
    CorrectionDirection,
    DirectorPlan,
    ReferenceTextCorrection,
)

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.25, 0.5, 1.0)


class OpenAiDirectorClient:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=settings.timeout_seconds,
        )
        self._owns_http = http_client is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def create_plan(self, *, source_text: str, target_language: LanguageCode) -> DirectorPlan:
        content = await self._post_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Return JSON only. Segment the supplied source by Python character "
                        "ranges. Do not return rewritten source text."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"source_text": source_text, "target_language": target_language},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
        )
        return _validate_payload(DirectorPlan, content)

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
    ) -> str:
        content = await self._post_json(
            [
                {
                    "role": "system",
                    "content": "Return JSON only with exactly ref_text_cn.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_ref_text_cn": current,
                            "direction": direction,
                            "emotion_description": emotion_description,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
        )
        return _validate_payload(ReferenceTextCorrection, content).ref_text_cn

    async def _post_json(self, messages: Sequence[dict[str, str]]) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if self._settings.mode == "openai":
            key_name = self._settings.api_key_env
            secret = os.environ.get(key_name or "")
            if not secret:
                raise PipelineError(
                    ErrorCode.LLM_UNAVAILABLE,
                    "llm",
                    "LLM API key is absent from its configured environment variable",
                    retryable=False,
                )
            headers["Authorization"] = f"Bearer {secret}"
        request_body: dict[str, object] = {
            "model": self._settings.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": list(messages),
        }
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._http.post(
                    "chat/completions", headers=headers, json=request_body
                )
            except httpx.HTTPError as exc:
                if attempt + 1 == attempts:
                    raise PipelineError(
                        ErrorCode.LLM_UNAVAILABLE,
                        "llm",
                        "LLM API request failed",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(_delay_for(attempt))
                continue
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                await asyncio.sleep(_delay_for(attempt))
                continue
            if response.status_code >= 400:
                raise PipelineError(
                    ErrorCode.LLM_UNAVAILABLE,
                    "llm",
                    f"LLM API returned HTTP {response.status_code}",
                    retryable=response.status_code in _RETRYABLE_STATUS_CODES,
                )
            try:
                envelope = response.json()
                message = envelope["choices"][0]["message"]["content"]
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                raise PipelineError(
                    ErrorCode.LLM_INVALID_RESPONSE,
                    "llm",
                    "LLM response lacks choices[0].message.content",
                    retryable=False,
                ) from exc
            return _decode_content(message)
        raise AssertionError("retry loop must return or raise")


def _delay_for(attempt: int) -> float:
    return _RETRY_DELAYS_SECONDS[min(attempt, len(_RETRY_DELAYS_SECONDS) - 1)]


def _decode_content(content: object) -> dict[str, object]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "LLM message content must be JSON object text",
            retryable=False,
        )
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "LLM message content is not valid JSON",
            retryable=False,
        ) from exc
    if not isinstance(value, dict):
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "LLM message content must be a JSON object",
            retryable=False,
        )
    return value


T = TypeVar("T", bound=BaseModel)


def _validate_payload(model: type[T], payload: dict[str, object]) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "LLM JSON does not match the required schema",
            retryable=False,
        ) from exc
