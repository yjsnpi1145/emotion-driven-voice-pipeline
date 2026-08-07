from __future__ import annotations

import json

import pytest

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.modules.audio.atomic_output import (
    atomic_write_json,
    reserve_output_path,
)
from voice_pipeline.modules.audio.wav_probe import sha256_file


def test_reservation_rejects_existing_target_without_modifying_it(tmp_path) -> None:
    target = tmp_path / "target.wav"
    target.write_bytes(b"sentinel")
    before = sha256_file(target)

    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        reserve_output_path(target)

    assert target.read_bytes() == b"sentinel"
    assert sha256_file(target) == before


def test_failed_publish_removes_only_owned_empty_reservation(tmp_path) -> None:
    target = tmp_path / "target.wav"
    reservation = reserve_output_path(target)
    reservation.rollback()
    assert not target.exists()


def test_publish_replaces_owned_reservation_with_partial(tmp_path) -> None:
    target = tmp_path / "target.wav"
    reservation = reserve_output_path(target)
    partial = tmp_path / "partial.wav"
    partial.write_bytes(b"audio-data")

    reservation.publish(partial)

    assert target.read_bytes() == b"audio-data"
    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        reservation.publish(partial)


def test_rollback_after_publish_does_not_delete_published_file(tmp_path) -> None:
    target = tmp_path / "target.wav"
    reservation = reserve_output_path(target)
    partial = tmp_path / "partial.wav"
    partial.write_bytes(b"audio-data")

    reservation.publish(partial)

    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        reservation.rollback()
    assert target.exists()
    assert target.read_bytes() == b"audio-data"


def test_rollback_does_not_touch_unrelated_files(tmp_path) -> None:
    target = tmp_path / "target.wav"
    reservation = reserve_output_path(target)
    other = tmp_path / "other.wav"
    other.write_bytes(b"other")
    reservation.rollback()
    assert not target.exists()
    assert other.read_bytes() == b"other"


def test_atomic_write_json_does_not_overwrite_existing(tmp_path) -> None:
    target = tmp_path / "manifest.json"
    target.write_bytes(b"sentinel")
    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        atomic_write_json(target, {"a": 1})
    assert target.read_bytes() == b"sentinel"


def test_atomic_write_json_writes_valid_utf8_json(tmp_path) -> None:
    target = tmp_path / "manifest.json"
    atomic_write_json(target, {"job_id": "x", "列表": [1, 2], "文本": "日本語"})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"job_id": "x", "列表": [1, 2], "文本": "日本語"}
