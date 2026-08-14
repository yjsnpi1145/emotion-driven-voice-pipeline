from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from voice_pipeline.core.config import LlmSettings
from voice_pipeline.core.errors import PipelineError
from voice_pipeline.models.director_llm import (
    AnalyzedUtterance,
    CastReconciliationResult,
    ChunkAnalysisResult,
    ScriptChunk,
    ScriptTranslationResult,
    TranslationInput,
)
from voice_pipeline.models.runtime_settings import (
    LlmConnectionTestResult,
    LlmSettingsSnapshot,
    LlmSettingsUpdate,
    LlmSettingsView,
)
from voice_pipeline.models.schemas import LanguageCode
from voice_pipeline.modules.llm.activity import LlmActivityLog, LlmOperation
from voice_pipeline.modules.llm.client import OpenAiDirectorClient
from voice_pipeline.modules.llm.fake import FakeDirector
from voice_pipeline.modules.llm.models import CorrectionDirection, DirectorPlan

_RUNTIME_KEY_ENV = "VOICE_PIPELINE_RUNTIME_LLM_KEY"


class _Director(Protocol):
    async def create_plan(
        self,
        *,
        source_text: str,
        target_language: LanguageCode,
        activity_id: UUID | None = None,
    ) -> DirectorPlan: ...

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
        activity_id: UUID | None = None,
    ) -> str: ...

    async def analyze_script_chunk(
        self, *, chunk: ScriptChunk, activity_id: UUID | None = None
    ) -> ChunkAnalysisResult: ...

    async def reconcile_cast(
        self,
        *,
        utterances: tuple[AnalyzedUtterance, ...],
        activity_id: UUID | None = None,
    ) -> CastReconciliationResult: ...

    async def translate_utterances(
        self,
        *,
        target_language: LanguageCode,
        utterances: tuple[TranslationInput, ...],
        activity_id: UUID | None = None,
    ) -> ScriptTranslationResult: ...


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
            max_parallel_requests=config.max_parallel_requests,
        )
        self._secret = os.environ.get(config.api_key_env or "") or None
        self._source: str = "config"
        self._director: _Director = FakeDirector()
        self.activity = LlmActivityLog()
        self._lock = asyncio.Lock()
        self._parallel_calls = asyncio.Semaphore(config.max_parallel_requests)
        self._active_staged_calls = 0
        self._staged_idle = asyncio.Event()
        self._staged_idle.set()
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
            self._parallel_calls = asyncio.Semaphore(self._snapshot.max_parallel_requests)
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
        await self._staged_idle.wait()
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
            self._parallel_calls = asyncio.Semaphore(snapshot.max_parallel_requests)
            await _close(previous)
            return self.view()

    async def test_connection(self, request: LlmSettingsUpdate) -> LlmConnectionTestResult:
        snapshot = request.snapshot()
        operation_id = uuid4()
        await self._record_started(operation_id, "connection_test", "开始测试 LLM 连接")
        try:
            async with self._lock:
                secret = self._updated_secret(request)
                candidate = self._build_director(snapshot, secret)
                try:
                    if isinstance(candidate, OpenAiDirectorClient):
                        latency_ms = await candidate.test_connection(activity_id=operation_id)
                    else:
                        latency_ms = 0
                finally:
                    await _close(candidate)
            result = LlmConnectionTestResult(
                base_url=snapshot.base_url,
                model=snapshot.model,
                latency_ms=latency_ms,
            )
        except BaseException as exc:
            await self._record_failed(operation_id, "connection_test", exc)
            raise
        await self._record_completed(
            operation_id,
            "connection_test",
            "LLM 连接测试完成",
            json.dumps(
                {"model": result.model, "latency_ms": result.latency_ms},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return result

    async def create_plan(self, *, source_text: str, target_language: LanguageCode) -> DirectorPlan:
        operation_id = uuid4()
        await self._record_started(operation_id, "chapter_plan", "开始规划章节分块")
        try:
            async with self._lock:
                result = await self._director.create_plan(
                    source_text=source_text,
                    target_language=target_language,
                    activity_id=operation_id,
                )
        except BaseException as exc:
            await self._record_failed(operation_id, "chapter_plan", exc)
            raise
        await self._record_completed(
            operation_id,
            "chapter_plan",
            f"章节规划完成 · {len(result.segments)} 个分块",
            result.model_dump_json(),
        )
        return result

    async def correct_reference_text(
        self,
        *,
        current: str,
        direction: CorrectionDirection,
        emotion_description: str,
    ) -> str:
        operation_id = uuid4()
        await self._record_started(operation_id, "reference_correction", "开始修正中文参考文本")
        try:
            async with self._lock:
                result = await self._director.correct_reference_text(
                    current=current,
                    direction=direction,
                    emotion_description=emotion_description,
                    activity_id=operation_id,
                )
        except BaseException as exc:
            await self._record_failed(operation_id, "reference_correction", exc)
            raise
        await self._record_completed(
            operation_id,
            "reference_correction",
            "中文参考文本修正完成",
            json.dumps({"ref_text_cn": result}, ensure_ascii=False, separators=(",", ":")),
        )
        return result

    async def analyze_script_chunk(self, *, chunk: ScriptChunk) -> ChunkAnalysisResult:
        operation_id = uuid4()
        await self._record_started(
            operation_id,
            "script_analysis",
            f"开始分析剧本块 · {chunk.source_start}:{chunk.source_end}",
        )
        try:
            async with self._parallel_calls:
                director = await self._begin_staged_call()
                try:
                    result = await director.analyze_script_chunk(
                        chunk=chunk, activity_id=operation_id
                    )
                finally:
                    await self._end_staged_call()
        except BaseException as exc:
            await self._record_failed(operation_id, "script_analysis", exc)
            raise
        await self._record_completed(
            operation_id,
            "script_analysis",
            f"剧本块分析完成 · {len(result.utterances)} 条语句",
            result.model_dump_json(),
        )
        return result

    async def reconcile_cast(
        self, *, utterances: tuple[AnalyzedUtterance, ...]
    ) -> CastReconciliationResult:
        operation_id = uuid4()
        await self._record_started(operation_id, "cast_reconciliation", "开始归并全局角色")
        try:
            async with self._parallel_calls:
                director = await self._begin_staged_call()
                try:
                    result = await director.reconcile_cast(
                        utterances=utterances, activity_id=operation_id
                    )
                finally:
                    await self._end_staged_call()
        except BaseException as exc:
            await self._record_failed(operation_id, "cast_reconciliation", exc)
            raise
        await self._record_completed(
            operation_id,
            "cast_reconciliation",
            f"角色归并完成 · {len(result.roles)} 个角色",
            result.model_dump_json(),
        )
        return result

    async def translate_utterances(
        self,
        *,
        target_language: LanguageCode,
        utterances: tuple[TranslationInput, ...],
    ) -> ScriptTranslationResult:
        operation_id = uuid4()
        await self._record_started(
            operation_id, "script_translation", f"开始翻译 {len(utterances)} 条语句"
        )
        try:
            async with self._parallel_calls:
                director = await self._begin_staged_call()
                try:
                    result = await director.translate_utterances(
                        target_language=target_language,
                        utterances=utterances,
                        activity_id=operation_id,
                    )
                finally:
                    await self._end_staged_call()
        except BaseException as exc:
            await self._record_failed(operation_id, "script_translation", exc)
            raise
        await self._record_completed(
            operation_id,
            "script_translation",
            f"剧本翻译完成 · {len(result.items)} 条语句",
            result.model_dump_json(),
        )
        return result

    async def aclose(self) -> None:
        await self._staged_idle.wait()
        async with self._lock:
            await _close(self._director)

    async def _begin_staged_call(self) -> _Director:
        async with self._lock:
            self._active_staged_calls += 1
            self._staged_idle.clear()
            return self._director

    async def _end_staged_call(self) -> None:
        async with self._lock:
            self._active_staged_calls -= 1
            if self._active_staged_calls == 0:
                self._staged_idle.set()

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
                max_parallel_requests=snapshot.max_parallel_requests,
            ),
            api_key=secret,
            activity=self.activity,
        )

    async def _record_started(
        self, operation_id: UUID, operation: LlmOperation, message: str
    ) -> None:
        await self.activity.emit(
            operation_id=operation_id,
            operation=operation,
            kind="started",
            message=message,
        )

    async def _record_completed(
        self,
        operation_id: UUID,
        operation: LlmOperation,
        message: str,
        content: str,
    ) -> None:
        await self.activity.emit(
            operation_id=operation_id,
            operation=operation,
            kind="completed",
            message=message,
            content=content,
        )

    async def _record_failed(
        self, operation_id: UUID, operation: LlmOperation, error: BaseException
    ) -> None:
        if isinstance(error, PipelineError):
            message = f"{error.code.value}: {error.message}"
        elif isinstance(error, asyncio.CancelledError):
            message = "LLM 操作已取消"
        else:
            message = "LLM 操作发生内部错误"
        await self.activity.emit(
            operation_id=operation_id,
            operation=operation,
            kind="failed",
            message=message,
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
