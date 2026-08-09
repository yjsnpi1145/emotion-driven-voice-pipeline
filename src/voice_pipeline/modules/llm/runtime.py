from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Protocol

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.models.runtime_settings import (
    LlmConnectionTestResult,
    LlmSettingsSnapshot,
    LlmSettingsUpdate,
    LlmSettingsView,
)
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.client import OpenAiDirectorClient
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.models import CorrectionDirection, DirectorPlan

_RUNTIME_KEY_ENV = "VOICE_PIPELINE_RUNTIME_LLM_KEY"


class _Director(Protocol):
    async def create_plan(
        self, *, source_text: str, target_language: LanguageCode
    ) -> DirectorPlan: ...

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
    ) -> str: ...


class RuntimeDirector:
    """Hot-swappable Director with local, atomic runtime settings persistence."""

    def __init__(self, config: LlmSettings, *, state_dir: Path) -> None:
        self._config = config
        self._state_dir = state_dir.resolve()
        self._settings_path = self._state_dir / "llm-settings.json"
        self._secret_path = self._state_dir / "llm-secret.txt"
        self._snapshot = LlmSettingsSnapshot(
            mode=config.mode,
            base_url=config.base_url,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            max_reference_corrections=config.max_reference_corrections,
        )
        self._secret = os.environ.get(config.api_key_env or "") or None
        self._source: str = "config"
        self._director: _Director = FakeDirector()
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            snapshot = await asyncio.to_thread(self._load_snapshot)
            if snapshot is not None:
                self._snapshot = snapshot
                self._source = "runtime"
            secret = await asyncio.to_thread(self._load_secret)
            if secret is not None:
                self._secret = secret
            self._director = self._build_director(self._snapshot, self._secret)
            self._started = True

    @property
    def max_reference_corrections(self) -> int:
        return self._snapshot.max_reference_corrections

    def view(self) -> LlmSettingsView:
        return LlmSettingsView(
            **self._snapshot.model_dump(),
            api_key_configured=self._secret is not None,
            source="runtime" if self._source == "runtime" else "config",
        )

    async def update(self, request: LlmSettingsUpdate) -> LlmSettingsView:
        async with self._lock:
            snapshot = request.snapshot()
            secret = self._updated_secret(request)
            replacement = self._build_director(snapshot, secret)
            try:
                await asyncio.to_thread(self._persist, snapshot, secret)
            except BaseException:
                await _close(replacement)
                raise
            previous = self._director
            self._snapshot = snapshot
            self._secret = secret
            self._source = "runtime"
            self._director = replacement
            await _close(previous)
            return self.view()

    async def test_connection(self, request: LlmSettingsUpdate) -> LlmConnectionTestResult:
        snapshot = request.snapshot()
        async with self._lock:
            secret = self._updated_secret(request)
            candidate = self._build_director(snapshot, secret)
            try:
                if isinstance(candidate, OpenAiDirectorClient):
                    latency_ms = await candidate.test_connection()
                else:
                    latency_ms = 0
            finally:
                await _close(candidate)
        return LlmConnectionTestResult(
            base_url=snapshot.base_url,
            model=snapshot.model,
            latency_ms=latency_ms,
        )

    async def create_plan(self, *, source_text: str, target_language: LanguageCode) -> DirectorPlan:
        async with self._lock:
            return await self._director.create_plan(
                source_text=source_text, target_language=target_language
            )

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
    ) -> str:
        async with self._lock:
            return await self._director.correct_reference_text(
                current=current,
                direction=direction,
                emotion_description=emotion_description,
            )

    async def aclose(self) -> None:
        async with self._lock:
            await _close(self._director)

    def _build_director(self, snapshot: LlmSettingsSnapshot, secret: str | None) -> _Director:
        if snapshot.mode == "fake":
            return FakeDirector()
        return OpenAiDirectorClient(
            LlmSettings(
                mode="openai",
                base_url=snapshot.base_url,
                model=snapshot.model,
                api_key_env=_RUNTIME_KEY_ENV,
                timeout_seconds=snapshot.timeout_seconds,
                max_retries=snapshot.max_retries,
                max_reference_corrections=snapshot.max_reference_corrections,
            ),
            api_key=secret,
        )

    def _updated_secret(self, request: LlmSettingsUpdate) -> str | None:
        if request.clear_api_key:
            return None
        if request.api_key is not None:
            return request.api_key.get_secret_value().strip()
        return self._secret

    def _load_snapshot(self) -> LlmSettingsSnapshot | None:
        try:
            return LlmSettingsSnapshot.model_validate_json(
                self._settings_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None

    def _load_secret(self) -> str | None:
        try:
            value = self._secret_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _persist(self, snapshot: LlmSettingsSnapshot, secret: str | None) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self._settings_path,
            json.dumps(
                snapshot.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        if secret is None:
            self._secret_path.unlink(missing_ok=True)
        else:
            _write_atomic(self._secret_path, secret)
            try:
                os.chmod(self._secret_path, 0o600)
            except OSError:
                pass


async def _close(director: object) -> None:
    close = getattr(director, "aclose", None)
    if close is not None:
        await close()


def _write_atomic(path: Path, content: str) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with open(partial, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
