from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.conftest import write_tone
from voice_pipeline.core.config import StorageSettings
from voice_pipeline.modules.cache.keys import canonical_key
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.cache_store import CacheStore
from voice_pipeline.storage.database import Database


@pytest.mark.asyncio
async def test_cache_put_hit_and_corruption_invalidation(tmp_path: Path) -> None:
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
    artifacts = ArtifactStore(runtime / "artifacts")
    cache = CacheStore(database, artifacts)
    try:
        source = tmp_path / "audio.wav"
        write_tone(source, seconds=4.0)
        blob = artifacts.publish_blob(artifacts.stage_audio(uuid4(), source))
        key = canonical_key("reference", {"text": "缓存测试", "seed": 1})

        await cache.put(key, blob)
        hit = await cache.get_valid(key)
        assert hit is not None
        assert hit.blob.content_sha256 == blob.content_sha256

        blob.absolute_path.write_bytes(b"corrupted")
        assert await cache.get_valid(key) is None
        assert (await cache.inspect())[0]["state"] == "invalid"
    finally:
        await database.close()
