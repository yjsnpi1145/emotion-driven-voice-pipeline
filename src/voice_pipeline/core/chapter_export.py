from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.chapter import ChapterRunRecord
from voice_pipeline.models.persistence import ArtifactVersionView, SegmentRecord
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.version_store import VersionStore


@dataclass(frozen=True, slots=True)
class ChapterGsvArchive:
    path: Path
    download_name: str

    def cleanup(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    ordinal: int
    segment_id: UUID
    version_id: UUID
    file_name: str
    content_sha256: str
    synthesis_text: str
    target_language: str
    ref_version_id: UUID
    source_path: Path

    def manifest_entry(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "segment_id": str(self.segment_id),
            "version_id": str(self.version_id),
            "file_name": self.file_name,
            "content_sha256": self.content_sha256,
            "synthesis_text": self.synthesis_text,
            "target_language": self.target_language,
            "ref_version_id": str(self.ref_version_id),
        }


class ChapterGsvArchiveBuilder:
    """Build one verified, disposable archive from current chapter GSV pointers."""

    def __init__(
        self,
        *,
        chapters: ChapterStore,
        versions: VersionStore,
        artifacts: ArtifactStore,
    ) -> None:
        self._chapters = chapters
        self._versions = versions
        self._artifacts = artifacts

    async def build(self, run_id: UUID) -> ChapterGsvArchive:
        run = await self._chapters.get(run_id)
        segments = await self._chapters.list_segments(run_id)
        missing_ordinals = [
            segment.ordinal for segment in segments if segment.active_gsv_version_id is None
        ]
        if missing_ordinals:
            raise PipelineError(
                ErrorCode.CHAPTER_STATE_CONFLICT,
                "chapter_export",
                "every chapter segment must have a current GSV version before export",
                retryable=False,
                details={"missing_ordinals": missing_ordinals},
            )

        width = max(3, len(str(len(segments))))
        entries: list[_ArchiveEntry] = []
        for position, segment in enumerate(segments, start=1):
            entries.append(
                await self._entry(
                    segment,
                    file_name=f"{position:0{width}d}.wav",
                )
            )

        title = _chapter_title(run)
        created_at = datetime.now(UTC).isoformat()
        archive_path = await asyncio.to_thread(
            _write_archive,
            self._artifacts.root,
            run.run_id,
            title,
            created_at,
            tuple(entries),
        )
        return ChapterGsvArchive(
            path=archive_path,
            download_name=f"{_safe_file_stem(title, run.run_id)}-gsv-segments.zip",
        )

    async def _entry(self, segment: SegmentRecord, *, file_name: str) -> _ArchiveEntry:
        version_id = segment.active_gsv_version_id
        if version_id is None:
            raise AssertionError("missing GSV pointers were rejected before entry resolution")
        try:
            version = await self._versions.get_version(version_id)
        except KeyError as exc:
            raise PipelineError(
                ErrorCode.ARTIFACT_MISSING,
                "chapter_export",
                "a current GSV version record is missing",
                retryable=False,
                details={"ordinal": segment.ordinal, "version_id": str(version_id)},
            ) from exc
        _validate_version(segment, version)
        source_path = _verified_blob_path(self._artifacts, version, segment.ordinal)
        synthesis_text = version.input_snapshot.get("text")
        if not isinstance(synthesis_text, str) or not synthesis_text.strip():
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "chapter_export",
                "a current GSV version has an invalid input snapshot",
                retryable=False,
                details={"ordinal": segment.ordinal, "version_id": str(version_id)},
            )
        if version.ref_version_id is None:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "chapter_export",
                "a current GSV version has no reference binding",
                retryable=False,
                details={"ordinal": segment.ordinal, "version_id": str(version_id)},
            )
        return _ArchiveEntry(
            ordinal=segment.ordinal,
            segment_id=segment.segment_id,
            version_id=version.version_id,
            file_name=file_name,
            content_sha256=version.blob_sha256,
            synthesis_text=synthesis_text,
            target_language=segment.target_language,
            ref_version_id=version.ref_version_id,
            source_path=source_path,
        )


def _validate_version(segment: SegmentRecord, version: ArtifactVersionView) -> None:
    if (
        version.segment_id != segment.segment_id
        or version.artifact_type != "gsv"
        or version.state != "ready"
    ):
        raise PipelineError(
            ErrorCode.ARTIFACT_CORRUPT,
            "chapter_export",
            "a current GSV pointer does not reference a ready GSV version",
            retryable=False,
            details={"ordinal": segment.ordinal, "version_id": str(version.version_id)},
        )


def _verified_blob_path(
    artifacts: ArtifactStore,
    version: ArtifactVersionView,
    ordinal: int,
) -> Path:
    root = artifacts.root.resolve()
    expected = artifacts.blob_path(version.blob_sha256).resolve()
    relative = version.blob_relative_path
    if relative.is_absolute() or ".." in relative.parts:
        raise _corrupt_blob(version, ordinal)
    candidate = root / relative
    if candidate.is_symlink():
        raise _corrupt_blob(version, ordinal)
    if not candidate.is_file():
        raise PipelineError(
            ErrorCode.ARTIFACT_MISSING,
            "chapter_export",
            "a current GSV audio blob is missing",
            retryable=False,
            details={"ordinal": ordinal, "version_id": str(version.version_id)},
        )
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to((root / "blobs").resolve())
    except ValueError as exc:
        raise _corrupt_blob(version, ordinal) from exc
    if resolved != expected or sha256_file(resolved) != version.blob_sha256:
        raise _corrupt_blob(version, ordinal)
    return resolved


def _corrupt_blob(version: ArtifactVersionView, ordinal: int) -> PipelineError:
    return PipelineError(
        ErrorCode.ARTIFACT_CORRUPT,
        "chapter_export",
        "a current GSV audio blob is corrupt",
        retryable=False,
        details={"ordinal": ordinal, "version_id": str(version.version_id)},
    )


def _write_archive(
    artifact_root: Path,
    run_id: UUID,
    title: str,
    created_at_utc: str,
    entries: tuple[_ArchiveEntry, ...],
) -> Path:
    export_root = artifact_root / "exports"
    export_root.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"{run_id}-",
        suffix=".zip",
        dir=export_root,
    )
    os.close(descriptor)
    archive_path = Path(raw_path)
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for entry in entries:
                _write_verified_file(archive, entry)
            manifest = {
                "schema_version": 1,
                "run_id": str(run_id),
                "title": title,
                "created_at_utc": created_at_utc,
                "segments": [entry.manifest_entry() for entry in entries],
            }
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    indent=2,
                ).encode("utf-8"),
            )
        return archive_path
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise


def _write_verified_file(archive: zipfile.ZipFile, entry: _ArchiveEntry) -> None:
    digest = hashlib.sha256()
    try:
        with entry.source_path.open("rb") as source, archive.open(entry.file_name, "w") as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
    except OSError as exc:
        raise PipelineError(
            ErrorCode.ARTIFACT_MISSING,
            "chapter_export",
            "a current GSV audio blob became unavailable during export",
            retryable=True,
            details={"ordinal": entry.ordinal, "version_id": str(entry.version_id)},
        ) from exc
    if digest.hexdigest() != entry.content_sha256:
        raise PipelineError(
            ErrorCode.ARTIFACT_CORRUPT,
            "chapter_export",
            "a current GSV audio blob changed during export",
            retryable=True,
            details={"ordinal": entry.ordinal, "version_id": str(entry.version_id)},
        )


def _chapter_title(run: ChapterRunRecord) -> str:
    request = run.snapshot.get("request")
    if isinstance(request, dict):
        title = request.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return f"chapter-{str(run.run_id)[:8]}"


def _safe_file_stem(title: str, run_id: UUID) -> str:
    without_controls = "".join(character for character in title if ord(character) >= 32)
    safe = re.sub(r'[<>:"/\\|?*]+', "_", without_controls).strip(" .")
    if not safe:
        safe = f"chapter-{str(run_id)[:8]}"
    return safe[:80].rstrip(" .")
