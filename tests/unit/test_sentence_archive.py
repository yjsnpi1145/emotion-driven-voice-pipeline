from __future__ import annotations

from zipfile import ZipFile

from voice_pipeline.core.sentence_archive import (
    SentenceArchiveEntry,
    sanitize_sentence_filename,
    write_sentence_archive,
)


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
