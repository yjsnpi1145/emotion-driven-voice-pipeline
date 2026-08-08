from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.model_profiles import ImportModelProfileRequest, ModelProfileRecord

_COPY_CHUNK_BYTES = 1024 * 1024


class ModelProfileImporter:
    def __init__(self, *, models_root: Path, allowed_import_roots: list[Path]) -> None:
        self._models_root = models_root.resolve()
        self._allowed_import_roots = [path.resolve() for path in allowed_import_roots]

    async def import_pair(self, request: ImportModelProfileRequest) -> ModelProfileRecord:
        return await asyncio.to_thread(self._import_pair_sync, request)

    def _import_pair_sync(self, request: ImportModelProfileRequest) -> ModelProfileRecord:
        gpt_source = self._validated_source(request.gpt_source_path, expected_suffix=".ckpt")
        sovits_source = self._validated_source(request.sovits_source_path, expected_suffix=".pth")
        profile_id = uuid4()
        staging = self._models_root / ".staging" / str(profile_id)
        final_directory = self._models_root / "profiles" / str(profile_id)
        if final_directory.exists():  # pragma: no cover - UUID collision defense
            raise PipelineError(
                ErrorCode.MODEL_IMPORT_FAILED,
                "model_import",
                "generated model profile directory already exists",
                retryable=True,
            )

        try:
            (staging / "GPT").mkdir(parents=True, exist_ok=False)
            (staging / "SoVITS").mkdir(parents=True, exist_ok=False)
            gpt_destination = staging / "GPT" / "model.ckpt"
            sovits_destination = staging / "SoVITS" / "model.pth"
            gpt_sha256, gpt_size_bytes = _copy_hash_fsync(gpt_source, gpt_destination)
            sovits_sha256, sovits_size_bytes = _copy_hash_fsync(sovits_source, sovits_destination)
            created_at = datetime.now(UTC)
            record = ModelProfileRecord(
                profile_id=profile_id,
                display_name=request.display_name,
                source_kind="imported",
                declared_family=request.declared_family,
                relative_directory=PurePosixPath("profiles", str(profile_id)),
                gpt_relative_path=PurePosixPath("profiles", str(profile_id), "GPT", "model.ckpt"),
                sovits_relative_path=PurePosixPath(
                    "profiles", str(profile_id), "SoVITS", "model.pth"
                ),
                gpt_sha256=gpt_sha256,
                sovits_sha256=sovits_sha256,
                gpt_size_bytes=gpt_size_bytes,
                sovits_size_bytes=sovits_size_bytes,
                created_at_utc=created_at,
            )
            _write_profile_json(
                staging / "profile.json",
                record=record,
                gpt_source=gpt_source,
                sovits_source=sovits_source,
            )
            _fsync_directory(staging / "GPT")
            _fsync_directory(staging / "SoVITS")
            _fsync_directory(staging)
            final_directory.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_directory)
            _fsync_directory(final_directory.parent)
            return record
        except PipelineError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise PipelineError(
                ErrorCode.MODEL_IMPORT_FAILED,
                "model_import",
                "could not copy model profile into local library",
                retryable=True,
            ) from exc

    def _validated_source(self, value: Path, *, expected_suffix: str) -> Path:
        try:
            source = value.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PipelineError(
                ErrorCode.MODEL_IMPORT_INVALID,
                "model_import",
                "model source file does not exist",
                retryable=False,
            ) from exc
        if (
            source.suffix.casefold() != expected_suffix
            or not source.is_file()
            or source.stat().st_size <= 0
        ):
            raise PipelineError(
                ErrorCode.MODEL_IMPORT_INVALID,
                "model_import",
                "model source must be a nonempty expected weight file",
                retryable=False,
            )
        if not any(_is_within(source, root) for root in self._allowed_import_roots):
            raise PipelineError(
                ErrorCode.MODEL_IMPORT_INVALID,
                "model_import",
                "model source is outside configured import roots",
                retryable=False,
            )
        return source


def _copy_hash_fsync(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as input_handle, open(destination, "xb") as output_handle:
        while chunk := input_handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            output_handle.write(chunk)
            size += len(chunk)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    return digest.hexdigest(), size


def _write_profile_json(
    path: Path,
    *,
    record: ModelProfileRecord,
    gpt_source: Path,
    sovits_source: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "profile_id": str(record.profile_id),
        "display_name": record.display_name,
        "source_kind": record.source_kind,
        "declared_family": record.declared_family,
        "created_at_utc": record.created_at_utc.isoformat(),
        "sources": {"gpt": str(gpt_source), "sovits": str(sovits_source)},
        "weights": {
            "gpt": {
                "relative_path": "GPT/model.ckpt",
                "sha256": record.gpt_sha256,
                "size_bytes": record.gpt_size_bytes,
            },
            "sovits": {
                "relative_path": "SoVITS/model.pth",
                "sha256": record.sovits_sha256,
                "size_bytes": record.sovits_size_bytes,
            },
        },
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
