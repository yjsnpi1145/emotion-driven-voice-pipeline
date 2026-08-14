from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.director import (
    CreateRolePresetRequest,
    RolePresetRecord,
    UpdateRolePresetRequest,
)
from voice_pipeline.models.model_profiles import ModelProfileSnapshot
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file
from voice_pipeline.storage.role_preset_store import RolePresetStore


class ModelProfileResolver(Protocol):
    async def get_ready_snapshot(self, profile_id: UUID) -> ModelProfileSnapshot: ...


class RolePresetService:
    def __init__(
        self,
        *,
        store: RolePresetStore,
        profiles: ModelProfileResolver,
        library_root: Path,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._library_root = library_root.resolve()

    async def import_preset(self, request: CreateRolePresetRequest) -> RolePresetRecord:
        source = request.base_voice_path.resolve(strict=True)
        if not source.is_file() or source.is_symlink() or source.suffix.casefold() != ".wav":
            raise PipelineError(
                ErrorCode.INVALID_AUDIO,
                "role_preset",
                "base voice must be a regular WAV file",
                retryable=False,
            )
        await self._profiles.get_ready_snapshot(request.model_profile_id)
        audio = await asyncio.to_thread(probe_wav, source, require_reference_window=False)
        preset_id = uuid4()
        relative = Path(str(preset_id)) / "base.wav"
        target = self._resolve_relative(relative.as_posix())
        await asyncio.to_thread(_copy_atomic, source, target)
        try:
            copied = await asyncio.to_thread(probe_wav, target, require_reference_window=False)
            if copied.content_sha256 != audio.content_sha256:
                raise PipelineError(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "role_preset",
                    "managed base voice hash differs from its source",
                    retryable=False,
                )
            now = datetime.now(UTC)
            return await self._store.insert(
                RolePresetRecord(
                    preset_id=preset_id,
                    name=request.name,
                    base_voice_relative_path=relative.as_posix(),
                    base_voice_sha256=copied.content_sha256,
                    byte_size=target.stat().st_size,
                    duration_seconds=copied.duration_seconds,
                    sample_rate=copied.sample_rate,
                    channels=copied.channels,
                    model_profile_id=request.model_profile_id,
                    default_speed=request.default_speed,
                    status="ready",
                    revision=0,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    async def list(self) -> list[RolePresetRecord]:
        return await self._store.list()

    async def get(self, preset_id: UUID) -> RolePresetRecord:
        return await self._store.get(preset_id)

    async def update(self, preset_id: UUID, request: UpdateRolePresetRequest) -> RolePresetRecord:
        if request.model_profile_id is not None:
            await self._profiles.get_ready_snapshot(request.model_profile_id)
        return await self._store.patch(
            preset_id,
            expected_revision=request.expected_revision,
            name=request.name,
            model_profile_id=request.model_profile_id,
            default_speed=request.default_speed,
        )

    async def archive(self, preset_id: UUID, *, expected_revision: int) -> RolePresetRecord:
        preset = await self._store.get(preset_id)
        if preset.revision != expected_revision:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "role_preset",
                "role preset changed; refresh before archiving",
                retryable=False,
            )
        return await self._store.update_status(preset_id, "archived")

    async def resolve(self, preset_id: UUID) -> RolePresetRecord:
        preset = await self._store.get(preset_id)
        if preset.status == "archived":
            return preset
        path = self.audio_path(preset)
        if not path.is_file():
            return await self._store.update_status(preset_id, "missing")
        try:
            digest = await asyncio.to_thread(sha256_file, path)
            if digest != preset.base_voice_sha256:
                return await self._store.update_status(preset_id, "corrupt")
            await asyncio.to_thread(probe_wav, path, require_reference_window=False)
            await self._profiles.get_ready_snapshot(preset.model_profile_id)
        except (OSError, PipelineError):
            return await self._store.update_status(preset_id, "corrupt")
        if preset.status != "ready":
            return await self._store.update_status(preset_id, "ready")
        return preset

    def audio_path(self, preset: RolePresetRecord) -> Path:
        return self._resolve_relative(preset.base_voice_relative_path)

    def _resolve_relative(self, relative: str) -> Path:
        candidate = (self._library_root / Path(relative)).resolve()
        try:
            candidate.relative_to(self._library_root)
        except ValueError as exc:
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "role_preset",
                "managed base voice path escapes the role library",
                retryable=False,
            ) from exc
        return candidate


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=False)
    partial = target.with_suffix(".partial")
    try:
        with open(source, "rb") as source_handle, open(partial, "xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
