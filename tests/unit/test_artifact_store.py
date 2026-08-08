from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.conftest import write_tone
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.artifact_store import ArtifactStore


@pytest.fixture
def tone(tmp_path: Path) -> Path:
    path = tmp_path / "源 音频.wav"
    write_tone(path, seconds=4.0)
    return path


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore((tmp_path / "artifact 空格 库").resolve())


def test_same_audio_reuses_content_addressed_blob(store: ArtifactStore, tone: Path) -> None:
    first = store.publish_blob(store.stage_audio(uuid4(), tone))
    second = store.publish_blob(store.stage_audio(uuid4(), tone))

    assert first.absolute_path == second.absolute_path
    assert second.reused_existing is True
    assert sha256_file(first.absolute_path) == first.content_sha256


def test_existing_wrong_content_is_never_overwritten(store: ArtifactStore, tone: Path) -> None:
    expected_sha = sha256_file(tone)
    destination = store.blob_path(expected_sha)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"sentinel")
    sentinel_sha = sha256_file(destination)

    with pytest.raises(PipelineError) as raised:
        store.publish_blob(store.stage_audio(uuid4(), tone))

    assert raised.value.code == ErrorCode.ARTIFACT_CORRUPT
    assert sha256_file(destination) == sentinel_sha


def test_materialized_job_copy_does_not_mutate_canonical_blob(
    store: ArtifactStore, tone: Path, tmp_path: Path
) -> None:
    blob = store.publish_blob(store.stage_audio(uuid4(), tone))
    destination = tmp_path / "jobs" / str(uuid4()) / "reference.wav"

    copied = store.materialize_job_output(blob, destination)
    copied.path.write_bytes(b"caller mutation")

    assert copied.path != blob.absolute_path
    assert sha256_file(blob.absolute_path) == blob.content_sha256


def test_manifest_publish_is_exclusive(store: ArtifactStore) -> None:
    relative = Path("manifests") / "a.json"
    first = store.publish_version_manifest(relative, {"version": 1})
    assert first.is_file()
    with pytest.raises(PipelineError) as raised:
        store.publish_version_manifest(relative, {"version": 2})
    assert raised.value.code == ErrorCode.OUTPUT_CONFLICT
