from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrency: int = 1
    queue_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def single_consumer_only(self) -> QueueSettings:
        if self.max_concurrency != 1:
            raise ValueError("max_concurrency must be exactly 1")
        return self


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    python_executable: Path
    repo_dir: Path
    request_timeout_seconds: float = Field(gt=0)
    expected_fingerprint: dict[str, str] | None = None


class EnginesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indextts: EngineSettings
    gpt_sovits: EngineSettings


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    mode: Literal["fake", "external_test", "real"]
    engine_lifecycle: Literal["resident", "exclusive_process"]
    server: ServerSettings
    runtime_dir: Path
    engine_lock_path: Path
    checkpoint_lock_path: Path
    queue: QueueSettings
    engines: EnginesSettings


def _resolve_path(base: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (base / value).resolve()


def load_settings(config_path: Path) -> AppSettings:
    resolved_config = config_path.resolve(strict=True)
    raw = yaml.safe_load(resolved_config.read_text(encoding="utf-8"))
    settings = AppSettings.model_validate(raw)
    base = resolved_config.parent
    settings.runtime_dir = _resolve_path(base, settings.runtime_dir)
    settings.engine_lock_path = _resolve_path(base, settings.engine_lock_path)
    settings.checkpoint_lock_path = _resolve_path(base, settings.checkpoint_lock_path)
    for engine in (settings.engines.indextts, settings.engines.gpt_sovits):
        engine.python_executable = _resolve_path(base, engine.python_executable)
        engine.repo_dir = _resolve_path(base, engine.repo_dir)
    return settings
