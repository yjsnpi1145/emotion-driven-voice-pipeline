from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from voice_pipeline.models.schemas import EngineFingerprint, WorkerName


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_sha256(paths: list[Path]) -> str:
    """Canonical bundle digest: sorted by basename, never absolute paths."""
    digest = hashlib.sha256()
    for name, path in sorted((p.name, p) for p in paths):
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_engine_lock(engine_lock_path: Path) -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(engine_lock_path.read_text(encoding="utf-8"))
    return dict(raw)


def compute_engine_fingerprint(
    engine: WorkerName,
    *,
    engine_lock_path: Path,
    checkpoint_lock_path: Path,
    env_lock_paths: list[Path],
    runtime_config_path: Path,
) -> EngineFingerprint:
    lock = load_engine_lock(engine_lock_path)
    if engine == "indextts":
        entry = lock["indextts"]
        source_revision = str(entry["revision"])
        model_revision = str(entry["model_revision"])
    else:
        entry = lock["gpt_sovits"]
        source_revision = str(entry["revision"])
        model_revision = str(entry["pretrained_revision"])
    return EngineFingerprint(
        schema_version=1,
        engine=engine,
        source_revision=source_revision,
        model_revision=model_revision,
        engine_lock_sha256=sha256_file(engine_lock_path),
        checkpoint_lock_sha256=sha256_file(checkpoint_lock_path),
        environment_lock_sha256=bundle_sha256(list(env_lock_paths)),
        runtime_config_sha256=sha256_file(runtime_config_path),
    )
