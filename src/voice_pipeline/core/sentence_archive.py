from __future__ import annotations

import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class SentenceArchiveEntry:
    ordinal: int
    source_text: str
    audio_path: Path


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
    return f"{ordinal + 1:04d}_{stem or '句子'}.wav"


def write_sentence_archive(
    entries: Sequence[SentenceArchiveEntry],
    output_path: Path,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(
        f".{output_path.stem}.{uuid4().hex}.partial{output_path.suffix}"
    )
    try:
        with ZipFile(partial, mode="x", compression=ZIP_DEFLATED) as bundle:
            for entry in entries:
                if not entry.audio_path.is_file() or entry.audio_path.is_symlink():
                    raise FileNotFoundError(entry.audio_path)
                info = ZipInfo(
                    sanitize_sentence_filename(entry.ordinal, entry.source_text),
                    date_time=_FIXED_ZIP_TIME,
                )
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                with entry.audio_path.open("rb") as source, bundle.open(info, "w") as target:
                    shutil.copyfileobj(source, target)
        os.replace(partial, output_path)
        return output_path
    finally:
        partial.unlink(missing_ok=True)
