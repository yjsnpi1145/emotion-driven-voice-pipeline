from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from voice_pipeline.core.config import StorageSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.model_profile_service import ModelProfileService
from voice_pipeline.models.model_profiles import ImportModelProfileRequest
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.model_importer import ModelProfileImporter
from voice_pipeline.storage.model_profile_store import SqliteModelProfileStore


async def _service(
    tmp_path: Path,
) -> tuple[Database, ModelProfileService, SqliteModelProfileStore, Path]:
    runtime = tmp_path / "runtime"
    database = await Database.open(
        StorageSettings(
            database_path=runtime / "state" / "pipeline.sqlite3",
            artifact_root=runtime / "artifacts",
            control_lock_path=runtime / "state" / "control.lock",
        ),
        instance_id=uuid4(),
        migrate=True,
    )
    root = tmp_path / "models" / "gpt-sovits"
    store = SqliteModelProfileStore(database, models_root=root)
    importer = ModelProfileImporter(models_root=root, allowed_import_roots=[tmp_path / "sources"])
    return database, ModelProfileService(importer=importer, store=store), store, root


@pytest.mark.asyncio
async def test_import_copies_pair_and_source_mutation_is_irrelevant(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    gpt = sources / "voice.ckpt"
    sovits = sources / "voice.pth"
    gpt.write_bytes(b"gpt-v1")
    sovits.write_bytes(b"sovits-v1")
    database, service, store, root = await _service(tmp_path)
    try:
        view = await service.import_profile(
            ImportModelProfileRequest(
                display_name="voice-v1",
                gpt_source_path=gpt.resolve(),
                sovits_source_path=sovits.resolve(),
            )
        )
        gpt.write_bytes(b"changed")
        snapshot = await store.get_ready_snapshot(view.profile_id)
        assert sha256_file(root / snapshot.gpt_relative_path) == view.gpt_sha256
        assert (root / snapshot.sovits_relative_path).is_file()
        assert sha256_file(gpt) != view.gpt_sha256
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_active_profile_resolves_to_verified_absolute_weight_paths(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    gpt = sources / "voice.ckpt"
    sovits = sources / "voice.pth"
    gpt.write_bytes(b"gpt-v1")
    sovits.write_bytes(b"sovits-v1")
    database, service, store, root = await _service(tmp_path)
    try:
        view = await service.import_profile(
            ImportModelProfileRequest(
                display_name="voice-v1",
                gpt_source_path=gpt.resolve(),
                sovits_source_path=sovits.resolve(),
            )
        )
        await service.activate_profile(view.profile_id)

        resolved = await store.resolve_selected_profile(None)

        assert resolved.profile_id == view.profile_id
        assert resolved.gpt_path == (root / resolved.gpt_relative_path).resolve()
        assert resolved.sovits_path == (root / resolved.sovits_relative_path).resolve()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_copy_has_no_database_row_or_visible_profile(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    gpt = sources / "voice.ckpt"
    gpt.write_bytes(b"gpt-v1")
    database, service, store, root = await _service(tmp_path)
    try:
        with pytest.raises(PipelineError) as raised:
            await service.import_profile(
                ImportModelProfileRequest(
                    display_name="voice-v1",
                    gpt_source_path=gpt.resolve(),
                    sovits_source_path=(sources / "missing.pth").resolve(),
                )
            )
        assert raised.value.code is ErrorCode.MODEL_IMPORT_INVALID
        assert await store.list() == []
        assert not (root / "profiles").exists()
    finally:
        await database.close()
