from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.sentence_archive import (
    SentenceArchiveEntry,
    sanitize_sentence_filename,
    write_sentence_archive,
)
from voice_pipeline.modules.audio.wav_probe import sha256_file


def _tone(path: Path, *, seconds: float, sample_rate: int = 32_000) -> None:
    time = np.arange(round(sample_rate * seconds)) / sample_rate
    data = (0.2 * np.sin(2 * np.pi * 223 * time)).astype(np.float32)
    sf.write(path, data, sample_rate, subtype="PCM_16")


def test_sentence_filename_is_ordered_sanitized_and_has_a_blank_fallback() -> None:
    assert sanitize_sentence_filename(0, '“为什么:这样/呢？”') == "0001_“为什么这样呢？”.wav"
    assert sanitize_sentence_filename(1, "  \n ") == "0002_句子.wav"
    assert sanitize_sentence_filename(11, "a" * 100, max_stem_chars=8) == "0012_aaaaaaaa.wav"


def test_sentence_archive_keeps_input_order_and_chinese_names(tmp_path) -> None:
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    archive = tmp_path / "nested" / "sentences.zip"

    result = write_sentence_archive(
        (
            SentenceArchiveEntry(ordinal=0, source_text="第一句", audio_path=first),
            SentenceArchiveEntry(ordinal=1, source_text="第一句", audio_path=second),
        ),
        archive,
    )

    assert result == archive
    with ZipFile(archive) as bundle:
        assert bundle.namelist() == ["0001_第一句.wav", "0002_第一句.wav"]
        assert bundle.read("0001_第一句.wav") == b"first"
        assert bundle.read("0002_第一句.wav") == b"second"
    assert not list(archive.parent.glob("*.partial.zip"))


def test_sentence_archive_appends_each_utterance_pause_without_mutating_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _tone(source, seconds=0.25)
    source_bytes = source.read_bytes()

    archive = write_sentence_archive(
        (
            SentenceArchiveEntry(
                ordinal=0,
                source_text="完整收尾",
                audio_path=source,
                pause_after_ms=450,
            ),
        ),
        tmp_path / "sentences.zip",
    )

    with ZipFile(archive) as bundle:
        exported_bytes = bundle.read("0001_完整收尾.wav")
    exported, sample_rate = sf.read(BytesIO(exported_bytes), dtype="float64")

    assert len(exported) / sample_rate == pytest.approx(0.70, abs=0.002)
    assert np.max(np.abs(exported[-round(sample_rate * 0.45) :])) == 0
    assert source.read_bytes() == source_bytes


def test_sentence_archive_rejects_a_blob_changed_after_version_resolution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    _tone(source, seconds=0.25)
    expected_sha256 = sha256_file(source)
    _tone(source, seconds=0.30)

    with pytest.raises(PipelineError) as raised:
        write_sentence_archive(
            (
                SentenceArchiveEntry(
                    ordinal=0,
                    source_text="校验音频",
                    audio_path=source,
                    pause_after_ms=300,
                    source_content_sha256=expected_sha256,
                ),
            ),
            tmp_path / "sentences.zip",
        )

    assert raised.value.code == ErrorCode.ARTIFACT_CORRUPT
    assert not (tmp_path / "sentences.zip").exists()
