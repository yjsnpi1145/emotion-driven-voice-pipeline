from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.role_preset_service import RolePresetService
from voice_pipeline.models.director import CreateRolePresetRequest
from voice_pipeline.models.model_profiles import ModelProfileRecord
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.model_profile_store import SqliteModelProfileStore
from voice_pipeline.storage.role_preset_store import RolePresetStore


@pytest.mark.asyncio
async def test_import_copies_wav_into_managed_library_and_detects_corruption(tmp_path: Path):
    runtime = tmp_path / "runtime"
    settings = StorageSettings(
        database_path=runtime / "state" / "pipeline.sqlite3",
        artifact_root=runtime / "artifacts",
        control_lock_path=runtime / "state" / "control.lock",
    )
    database = await Database.open(settings, instance_id=uuid4(), migrate=True)
    try:
        models_root = runtime / "models"
        profile_dir = models_root / "profiles" / "one"
        profile_dir.mkdir(parents=True)
        gpt = profile_dir / "voice.ckpt"
        sovits = profile_dir / "voice.pth"
        gpt.write_bytes(b"gpt")
        sovits.write_bytes(b"sovits")
        profile_id = uuid4()
        profile_store = SqliteModelProfileStore(database, models_root=models_root)
        await profile_store.insert_published(
            ModelProfileRecord(
                profile_id=profile_id,
                display_name="角色模型",
                source_kind="imported",
                relative_directory=PurePosixPath("profiles/one"),
                gpt_relative_path=PurePosixPath("profiles/one/voice.ckpt"),
                sovits_relative_path=PurePosixPath("profiles/one/voice.pth"),
                gpt_sha256=sha256_file(gpt),
                sovits_sha256=sha256_file(sovits),
                gpt_size_bytes=gpt.stat().st_size,
                sovits_size_bytes=sovits.stat().st_size,
                status="ready",
                created_at_utc=datetime.now(UTC),
            )
        )
        source = tmp_path / "long-base.wav"
        write_tone(source, 15.0)
        store = RolePresetStore(database)
        service = RolePresetService(
            store=store,
            profiles=profile_store,
            library_root=settings.artifact_root / "role-presets",
        )
        preset = await service.import_preset(
            CreateRolePresetRequest(
                name="甲",
                base_voice_path=source.resolve(),
                model_profile_id=profile_id,
                default_speed=1.1,
            )
        )
        managed = service.audio_path(preset)
        assert managed.is_file()
        assert managed != source
        assert preset.duration_seconds > 10.0
        source.unlink()
        assert (await service.resolve(preset.preset_id)).status == "ready"

        managed.write_bytes(b"broken")
        assert (await service.resolve(preset.preset_id)).status == "corrupt"
    finally:
        await database.close()
