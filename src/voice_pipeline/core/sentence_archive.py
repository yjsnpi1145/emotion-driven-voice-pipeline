from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.audio.wav_export import append_trailing_silence

_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_SHELL_MAX_FILENAME_BYTES = 160
_DOS_ARCHIVE_ATTRIBUTE = 0x20


@dataclass(frozen=True, slots=True)
class SentenceArchiveEntry:
    ordinal: int
    source_text: str
    audio_path: Path
    pause_after_ms: int = 0
    source_content_sha256: str | None = None


def sanitize_sentence_filename(
    ordinal: int,
    source_text: str,
    *,
    max_stem_chars: int = 80,
) -> str:
    if ordinal < 0:
        raise ValueError("sentence ordinal must be non-negative")
    if max_stem_chars < 1:
        raise ValueError("max_stem_chars must be positive")
    stem = _WINDOWS_INVALID.sub("", " ".join(source_text.split())).strip(" .")
    stem = stem[:max_stem_chars].rstrip(" .")
    prefix = f"{ordinal + 1:04d}_"
    suffix = ".wav"
    byte_budget = _WINDOWS_SHELL_MAX_FILENAME_BYTES - len(f"{prefix}{suffix}".encode())
    stem = _truncate_utf8(stem or "句子", byte_budget).rstrip(" .") or "句子"
    return f"{prefix}{stem}{suffix}"


def _truncate_utf8(value: str, byte_budget: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_budget:
        return value
    return encoded[:byte_budget].decode("utf-8", errors="ignore")


def _current_zip_time() -> tuple[int, int, int, int, int, int]:
    now = datetime.now()
    return (now.year, now.month, now.day, now.hour, now.minute, now.second)


def write_sentence_archive(
    entries: Sequence[SentenceArchiveEntry],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(
        f".{output_path.stem}.{uuid4().hex}.partial{output_path.suffix}"
    )
    archive_time = _current_zip_time()
    try:
        with ZipFile(partial, mode="x", compression=ZIP_DEFLATED) as bundle:
            for entry in entries:
                details = {"ordinal": entry.ordinal}
                if entry.audio_path.is_symlink():
                    raise PipelineError(
                        ErrorCode.ARTIFACT_CORRUPT,
                        "sentence_archive",
                        "sentence audio source is a symlink",
                        retryable=False,
                        details=details,
                    )
                if not entry.audio_path.is_file():
                    raise PipelineError(
                        ErrorCode.ARTIFACT_MISSING,
                        "sentence_archive",
                        "sentence audio source is missing",
                        retryable=False,
                        details=details,
                    )
                info = ZipInfo(
                    sanitize_sentence_filename(entry.ordinal, entry.source_text),
                    date_time=archive_time,
                )
                info.compress_type = ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = _DOS_ARCHIVE_ATTRIBUTE
                try:
                    source_wav = entry.audio_path.read_bytes()
                except OSError as exc:
                    raise PipelineError(
                        ErrorCode.ARTIFACT_MISSING,
                        "sentence_archive",
                        "sentence audio source became unavailable",
                        retryable=True,
                        details=details,
                    ) from exc
                if (
                    entry.source_content_sha256 is not None
                    and hashlib.sha256(source_wav).hexdigest() != entry.source_content_sha256
                ):
                    raise PipelineError(
                        ErrorCode.ARTIFACT_CORRUPT,
                        "sentence_archive",
                        "sentence audio source changed before export",
                        retryable=True,
                        details=details,
                    )
                try:
                    exported_wav = append_trailing_silence(
                        source_wav,
                        silence_ms=entry.pause_after_ms,
                    )
                except (RuntimeError, ValueError) as exc:
                    raise PipelineError(
                        ErrorCode.ARTIFACT_CORRUPT,
                        "sentence_archive",
                        "sentence audio source is not a valid standalone WAV",
                        retryable=False,
                        details=details,
                    ) from exc
                with bundle.open(info, "w") as target:
                    target.write(exported_wav)
        os.replace(partial, output_path)
        return output_path
    finally:
        partial.unlink(missing_ok=True)
