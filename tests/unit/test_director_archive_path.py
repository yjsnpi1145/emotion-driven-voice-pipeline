from __future__ import annotations

from pathlib import Path

import pytest

from voice_pipeline.api.director_routes import _resolve_sentence_archive_path


def test_sentence_archive_path_rejects_the_archive_symlink_before_resolving(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "artifacts"
    output = root / "directors" / "generation"
    output.mkdir(parents=True)
    (output / "final.wav").write_bytes(b"RIFF")
    target = output / "sentences-real.zip"
    target.write_bytes(b"PK")
    candidate = output / "sentences.zip"
    candidate.write_bytes(b"link fixture")
    original_resolve = Path.resolve
    original_is_symlink = Path.is_symlink

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == candidate:
            return target
        return original_resolve(path, strict=strict)

    def is_symlink(path: Path) -> bool:
        return path == candidate or original_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    with pytest.raises(ValueError, match="symlink"):
        _resolve_sentence_archive_path(
            root,
            "directors/generation/final.wav",
        )
