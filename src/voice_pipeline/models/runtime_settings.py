from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from voice_pipeline.models.schemas import NonBlankText, StrictModel


class LlmSettingsSnapshot(StrictModel):
    mode: Literal["fake", "openai"] = "fake"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: NonBlankText = "fake-director"
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_reference_corrections: int = Field(default=2, ge=0, le=5)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain credentials, query, or fragment")
        return normalized


class LlmSettingsView(LlmSettingsSnapshot):
    api_key_configured: bool
    source: Literal["config", "runtime"]


class LlmSettingsUpdate(LlmSettingsSnapshot):
    api_key: SecretStr | None = None
    clear_api_key: bool = False

    @model_validator(mode="after")
    def key_update_is_unambiguous(self) -> LlmSettingsUpdate:
        if self.clear_api_key and self.api_key is not None:
            raise ValueError("api_key and clear_api_key cannot be supplied together")
        if self.api_key is not None and not self.api_key.get_secret_value().strip():
            raise ValueError("api_key must not be blank")
        return self

    def snapshot(self) -> LlmSettingsSnapshot:
        return LlmSettingsSnapshot.model_validate(
            self.model_dump(exclude={"api_key", "clear_api_key"})
        )


class LlmConnectionTestResult(StrictModel):
    ok: Literal[True] = True
    base_url: str
    model: str
    latency_ms: int = Field(ge=0)
