from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from time import perf_counter
from typing import TypeVar
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ValidationError

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.activity import (
    LlmActivityKind,
    LlmActivityLog,
    LlmOperation,
)
from voice_pipeline.modules.llm.models import (
    CorrectionDirection,
    DirectorPlan,
    ReferenceTextCorrection,
)

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.25, 0.5, 1.0)


def _director_system_prompt(
    *, source_sha256: str, source_length: int, target_language: LanguageCode
) -> str:
    schema = json.dumps(
        DirectorPlan.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "Return exactly one JSON object and no markdown. The object MUST validate against "
        f"this JSON Schema: {schema}\n"
        f"Echo source_text_sha256 exactly as {source_sha256}. The source has exactly "
        f"{source_length} Python Unicode characters and target_language is {target_language}. "
        "segments must be non-empty, ordinal must start at 0 and increase by 1, source_start "
        "and source_end are Python string-slice indices, the first source_start must be 0, "
        "every source_start must equal the previous source_end, and the final source_end must "
        f"be {source_length}. Never return a source_text field or alter the source ranges. "
        "For every segment provide all fields: ordinal, source_start, source_end, "
        "emotion_description, emotion_vector, synthesis_text, ref_text_cn, pause_after_ms, "
        "speed_factor, seed. Automatically detect the language of each source slice. "
        "synthesis_text must be written in target_language and must translate the complete "
        "source slice without summary, explanation, omission, or duplication. If the source "
        "slice is already written in target_language, copy it faithfully instead of translating "
        "it into another language. Across all ordered segments, synthesis_text must render the "
        "entire submitted source. "
        "emotion_vector contains exactly 8 numbers ordered as joy, anger, sadness, fear, "
        "disgust, melancholy, surprise, calm; every value is within 0.0..1.0 and their sum "
        "must be <= 0.8. ref_text_cn must always be natural Simplified Chinese that expresses "
        "the source segment meaning and emotion and is suitable for roughly 3 to 10 seconds of "
        "speech. For ref_text_cn, never copy Japanese, English, Korean, Cantonese romanization, "
        "or any other non-Mandarin source text; translate or adapt it into written Mandarin "
        "Chinese even when source_text or target_language is not Chinese. "
        "pause_after_ms must be an integer within 0..30000, speed_factor within 0.5..2.0, "
        "and seed must be an integer."
    )


class OpenAiDirectorClient:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
        activity: LlmActivityLog | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        self._http = http_client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            timeout=settings.timeout_seconds,
        )
        self._owns_http = http_client is None
        self._activity = activity

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def create_plan(
        self,
        *,
        source_text: str,
        target_language: LanguageCode,
        activity_id: UUID | None = None,
    ) -> DirectorPlan:
        source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        content = await self._post_json(
            [
                {
                    "role": "system",
                    "content": _director_system_prompt(
                        source_sha256=source_sha256,
                        source_length=len(source_text),
                        target_language=target_language,
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_text": source_text,
                            "source_text_sha256": source_sha256,
                            "source_length": len(source_text),
                            "target_language": target_language,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            operation_id=activity_id or uuid4(),
            operation="chapter_plan",
        )
        return _validate_payload(DirectorPlan, content)

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
        activity_id: UUID | None = None,
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
            ],
            operation_id=activity_id or uuid4(),
            operation="reference_correction",
        )
        return _validate_payload(ReferenceTextCorrection, content).ref_text_cn

    async def test_connection(self, *, activity_id: UUID | None = None) -> int:
        started = perf_counter()
        content = await self._post_json(
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": 'Return exactly {"ok": true}.'},
            ],
            operation_id=activity_id or uuid4(),
            operation="connection_test",
        )
        if content.get("ok") is not True:
            raise PipelineError(
                ErrorCode.LLM_INVALID_RESPONSE,
                "llm",
                "LLM connection test did not return ok=true",
                retryable=False,
            )
        return max(0, round((perf_counter() - started) * 1000))

    async def _post_json(
        self,
        messages: Sequence[dict[str, str]],
        *,
        operation_id: UUID,
        operation: LlmOperation,
    ) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if self._settings.mode == "openai":
            key_name = self._settings.api_key_env
            secret = self._api_key or os.environ.get(key_name or "")
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
            await self._emit(
                operation_id=operation_id,
                operation=operation,
                kind="request_sent",
                message=f"请求已发送（第 {attempt + 1}/{attempts} 次）· {self._settings.model}",
            )
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
                await self._emit(
                    operation_id=operation_id,
                    operation=operation,
                    kind="retrying",
                    message=f"网络请求失败，{_delay_for(attempt):.2f} 秒后重试",
                )
                await asyncio.sleep(_delay_for(attempt))
                continue
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                await self._emit(
                    operation_id=operation_id,
                    operation=operation,
                    kind="retrying",
                    message=(
                        f"模型返回 HTTP {response.status_code}，"
                        f"{_delay_for(attempt):.2f} 秒后重试"
                    ),
                )
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
            await self._emit(
                operation_id=operation_id,
                operation=operation,
                kind="response",
                message=f"已收到模型响应 · HTTP {response.status_code}",
                content=_render_output(message),
            )
            return _decode_content(message)
        raise AssertionError("retry loop must return or raise")

    async def _emit(
        self,
        *,
        operation_id: UUID,
        operation: LlmOperation,
        kind: LlmActivityKind,
        message: str,
        content: str | None = None,
    ) -> None:
        if self._activity is None:
            return
        await self._activity.emit(
            operation_id=operation_id,
            operation=operation,
            kind=kind,
            message=message,
            content=content,
        )


def _delay_for(attempt: int) -> float:
    return _RETRY_DELAYS_SECONDS[min(attempt, len(_RETRY_DELAYS_SECONDS) - 1)]


def _render_output(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        schema_errors = [
            {
                "path": ".".join(str(part) for part in error["loc"]),
                "type": str(error["type"]),
            }
            for error in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )[:20]
        ]
        raise PipelineError(
            ErrorCode.LLM_INVALID_RESPONSE,
            "llm",
            "LLM JSON does not match the required schema",
            retryable=False,
            details={"schema_errors": schema_errors},
        ) from exc
