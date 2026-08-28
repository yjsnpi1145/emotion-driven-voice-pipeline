from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from uuid import UUID

from pydantic import Field, field_validator

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import AudioResult, StrictModel
from voice_pipeline.modules.audio.atomic_output import atomic_write_json, reserve_output_path
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file


class StagedArtifact(StrictModel):
    path: Path
    job_id: UUID
    audio: AudioResult
    byte_size: int = Field(gt=0)


class PublishedBlob(StrictModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_path: Path
    absolute_path: Path
    byte_size: int = Field(gt=0)
    audio: AudioResult
    reused_existing: bool

    @field_validator("absolute_path")
    @classmethod
    def absolute_path_required(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("artifact blob path must be absolute")
        return value


class ArtifactStore:
    """Content-addressed immutable WAV blobs with copy-only job materialization."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or _is_unc(root):
            raise ValueError("artifact root must be an absolute local path")
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def blob_path(self, content_sha256: str) -> Path:
        _require_sha256(content_sha256)
        return self._root / "blobs" / "sha256" / content_sha256[:2] / f"{content_sha256}.wav"

    def verified_blob_path(
        self,
        *,
        content_sha256: str,
        relative_path: Path,
    ) -> Path:
        """Resolve a persisted blob reference without trusting its stored path."""
        _require_sha256(content_sha256)
        relative = PurePosixPath(relative_path.as_posix())
        if relative.is_absolute() or ".." in relative.parts:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "artifact version path escapes the managed blob store",
                retryable=False,
            )
        expected = self.blob_path(content_sha256)
        expected_relative = PurePosixPath(expected.relative_to(self._root).as_posix())
        if relative != expected_relative:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "artifact version path does not match its content-addressed blob",
                retryable=False,
            )
        candidate = self._root / Path(relative)
        if candidate.is_symlink():
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "artifact version points to a symlink",
                retryable=False,
            )
        if not candidate.is_file():
            raise PipelineError(
                ErrorCode.ARTIFACT_MISSING,
                "artifact",
                "artifact version blob is missing",
                retryable=False,
            )
        try:
            resolved = candidate.resolve(strict=True)
            actual_sha256 = sha256_file(resolved)
        except OSError as exc:
            raise PipelineError(
                ErrorCode.ARTIFACT_MISSING,
                "artifact",
                "artifact version blob became unavailable",
                retryable=True,
            ) from exc
        if resolved != expected or actual_sha256 != content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "artifact version no longer matches its content-addressed blob",
                retryable=False,
            )
        return resolved

    def stage_audio(self, job_id: UUID, source_path: Path) -> StagedArtifact:
        source = _require_regular_absolute_file(source_path, label="audio source")
        source_sha = sha256_file(source)
        target = self._root / "staging" / str(job_id) / f"{uuid.uuid4()}.wav"
        reservation = reserve_output_path(target)
        partial = target.with_name(f".{target.stem}.{uuid.uuid4()}.partial.wav")
        try:
            _copy_and_fsync(source, partial)
            if sha256_file(partial) != source_sha:
                raise PipelineError(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "artifact",
                    "staged audio hash does not match source audio",
                    retryable=False,
                )
            audio = probe_wav(partial, require_reference_window=False)
            reservation.publish(partial)
            return StagedArtifact(
                path=target,
                job_id=job_id,
                audio=audio.model_copy(update={"path": target}),
                byte_size=target.stat().st_size,
            )
        except BaseException:
            reservation.rollback()
            raise
        finally:
            partial.unlink(missing_ok=True)

    def publish_blob(self, staged: StagedArtifact) -> PublishedBlob:
        self._verify_staged(staged)
        content_sha256 = staged.audio.content_sha256
        destination = self.blob_path(content_sha256)
        if destination.exists():
            published = self._existing_blob(destination, staged)
            staged.path.unlink(missing_ok=True)
            return published

        try:
            reservation = reserve_output_path(destination)
        except PipelineError as exc:
            if exc.code != ErrorCode.OUTPUT_CONFLICT or not destination.exists():
                raise
            published = self._existing_blob(destination, staged)
            staged.path.unlink(missing_ok=True)
            return published
        try:
            reservation.publish(staged.path)
        except BaseException:
            reservation.rollback()
            raise
        return self._published(destination, staged, reused_existing=False)

    def materialize_job_output(self, blob: PublishedBlob, destination: Path) -> AudioResult:
        canonical = self._verify_published_blob(blob)
        if not destination.is_absolute() or _is_unc(destination):
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "artifact",
                "job output destination must be an absolute local path",
                retryable=False,
            )
        target = destination.resolve()
        reservation = reserve_output_path(target)
        partial = target.with_name(f".{target.stem}.{uuid.uuid4()}.partial.wav")
        try:
            _copy_and_fsync(canonical, partial)
            if sha256_file(partial) != blob.content_sha256:
                raise PipelineError(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "artifact",
                    "materialized job output hash does not match canonical blob",
                    retryable=False,
                )
            audio = probe_wav(partial, require_reference_window=False)
            reservation.publish(partial)
            return audio.model_copy(update={"path": target})
        except BaseException:
            reservation.rollback()
            raise
        finally:
            partial.unlink(missing_ok=True)

    def publish_version_manifest(self, relative_path: Path, payload: object) -> Path:
        relative = PurePosixPath(relative_path.as_posix())
        if relative.is_absolute() or ".." in relative.parts:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "artifact",
                "version manifest path must be relative and contained",
                retryable=False,
            )
        target = (self._root / relative).resolve()
        _require_within(target, self._root, label="version manifest")
        atomic_write_json(target, payload)
        return target

    def _verify_staged(self, staged: StagedArtifact) -> None:
        path = _require_regular_absolute_file(staged.path, label="staged audio")
        _require_within(path, self._root / "staging", label="staged audio")
        if path.is_symlink() or sha256_file(path) != staged.audio.content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "staged audio no longer matches its verified content hash",
                retryable=False,
            )
        if path.stat().st_size != staged.byte_size:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "staged audio byte size changed after validation",
                retryable=False,
            )

    def _existing_blob(self, destination: Path, staged: StagedArtifact) -> PublishedBlob:
        if destination.is_symlink() or not destination.is_file():
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "content-addressed blob path is not a regular file",
                retryable=False,
            )
        if sha256_file(destination) != staged.audio.content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "existing content-addressed blob has different content",
                retryable=False,
            )
        existing_audio = probe_wav(destination, require_reference_window=False)
        return PublishedBlob(
            content_sha256=staged.audio.content_sha256,
            relative_path=destination.relative_to(self._root),
            absolute_path=destination,
            byte_size=destination.stat().st_size,
            audio=existing_audio,
            reused_existing=True,
        )

    def _published(
        self, destination: Path, staged: StagedArtifact, *, reused_existing: bool
    ) -> PublishedBlob:
        if sha256_file(destination) != staged.audio.content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "published blob hash does not match staged audio",
                retryable=False,
            )
        audio = probe_wav(destination, require_reference_window=False)
        return PublishedBlob(
            content_sha256=staged.audio.content_sha256,
            relative_path=destination.relative_to(self._root),
            absolute_path=destination,
            byte_size=destination.stat().st_size,
            audio=audio,
            reused_existing=reused_existing,
        )

    def _verify_published_blob(self, blob: PublishedBlob) -> Path:
        path = _require_regular_absolute_file(blob.absolute_path, label="canonical blob")
        _require_within(path, self._root / "blobs", label="canonical blob")
        if path.is_symlink() or sha256_file(path) != blob.content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact",
                "canonical blob is missing or corrupt",
                retryable=False,
            )
        return path


def _copy_and_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as source_handle, open(destination, "xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("content SHA-256 must be lowercase hexadecimal")


def _require_regular_absolute_file(path: Path, *, label: str) -> Path:
    if not path.is_absolute() or _is_unc(path):
        raise PipelineError(
            ErrorCode.INVALID_INPUT,
            "artifact",
            f"{label} must be an absolute local path",
            retryable=False,
        )
    if path.is_symlink() or not path.is_file():
        raise PipelineError(
            ErrorCode.INVALID_INPUT,
            "artifact",
            f"{label} must be a regular non-symlink file",
            retryable=False,
        )
    return path.resolve(strict=True)


def _require_within(path: Path, root: Path, *, label: str) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise PipelineError(
            ErrorCode.INVALID_INPUT,
            "artifact",
            f"{label} escapes its managed root",
            retryable=False,
        ) from exc


def _is_unc(path: Path) -> bool:
    return str(path).startswith("\\\\")
