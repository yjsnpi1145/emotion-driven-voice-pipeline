from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from voice_pipeline.modules.quality.models import QualityPolicy


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


class ModelLibrarySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models_root: Path = Path("../models/gpt-sovits")
    allowed_import_roots: list[Path] = Field(default_factory=list)

    def validate_against_runtime(self, runtime_dir: Path) -> ModelLibrarySettings:
        if self.models_root == runtime_dir or _is_within(self.models_root, runtime_dir):
            raise ValueError("models_root must not overlap runtime_dir")
        return self


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database_path: Path = Path("state/pipeline.sqlite3")
    artifact_root: Path = Path("artifacts")
    control_lock_path: Path = Path("state/control.lock")
    busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    wal_autocheckpoint_pages: int = Field(default=1000, ge=1)
    history_limit: Literal[5] = 5
    cache_max_entries_per_kind: int = Field(default=500, ge=10)
    cache_max_age_days: int = Field(default=90, ge=1)

    def resolve_against_runtime(self, runtime_dir: Path) -> StorageSettings:
        for field_name in ("database_path", "artifact_root", "control_lock_path"):
            raw = getattr(self, field_name)
            resolved = raw.resolve() if raw.is_absolute() else (runtime_dir / raw).resolve()
            if str(resolved).startswith("\\\\") or not _is_within(resolved, runtime_dir):
                raise ValueError(f"{field_name} must be a local path within runtime_dir")
            setattr(self, field_name, resolved)
        return self


class LlmSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fake", "openai"] = "fake"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "fake-director"
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_reference_corrections: int = Field(default=2, ge=0, le=5)
    max_parallel_requests: int = Field(default=3, ge=1, le=8)

    @model_validator(mode="after")
    def require_api_key_environment_variable(self) -> LlmSettings:
        if self.mode == "openai" and not (self.api_key_env or "").strip():
            raise ValueError("llm.api_key_env is required for openai mode")
        return self


class QualitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["fake", "faster_whisper"] = "fake"
    model_path: Path = Path("../runtime/models/faster-whisper-small")
    model_lock_path: Path = Path("quality-model.lock.yaml")
    policy: QualityPolicy = Field(default_factory=QualityPolicy)


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
    model_library: ModelLibrarySettings = Field(default_factory=ModelLibrarySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)

    @model_validator(mode="after")
    def require_real_quality_adapter(self) -> AppSettings:
        if self.mode == "real" and "quality" not in self.model_fields_set:
            self.quality = QualitySettings(mode="faster_whisper")
        if self.mode == "real" and self.quality.mode != "faster_whisper":
            raise ValueError("real mode requires quality.mode=faster_whisper")
        if self.mode != "real" and self.quality.mode != "fake":
            raise ValueError("fake and external_test modes require quality.mode=fake")
        return self


def _resolve_path(base: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (base / value).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_settings(config_path: Path) -> AppSettings:
    resolved_config = config_path.resolve(strict=True)
    raw = yaml.safe_load(resolved_config.read_text(encoding="utf-8"))
    settings = AppSettings.model_validate(raw)
    base = resolved_config.parent
    settings.runtime_dir = _resolve_path(base, settings.runtime_dir)
    settings.engine_lock_path = _resolve_path(base, settings.engine_lock_path)
    settings.checkpoint_lock_path = _resolve_path(base, settings.checkpoint_lock_path)
    settings.model_library.models_root = _resolve_path(base, settings.model_library.models_root)
    settings.model_library.allowed_import_roots = [
        _resolve_path(base, root) for root in settings.model_library.allowed_import_roots
    ]
    settings.model_library.validate_against_runtime(settings.runtime_dir)
    settings.storage.resolve_against_runtime(settings.runtime_dir)
    settings.quality.model_path = _resolve_path(base, settings.quality.model_path)
    settings.quality.model_lock_path = _resolve_path(base, settings.quality.model_lock_path)
    for engine in (settings.engines.indextts, settings.engines.gpt_sovits):
        engine.python_executable = _resolve_path(base, engine.python_executable)
        engine.repo_dir = _resolve_path(base, engine.repo_dir)
    return settings
