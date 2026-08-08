from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file
from voice_pipeline.modules.cache.keys import CanonicalCacheKey
from voice_pipeline.storage.artifact_store import ArtifactStore, PublishedBlob
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import artifact_blobs, cache_entries


class CacheHit:
    def __init__(self, blob: PublishedBlob) -> None:
        self.blob = blob


class CacheStore:
    """Cache index whose reusable unit is a verified canonical audio blob."""

    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self._database = database
        self._artifacts = artifacts

    async def get_valid(self, key: CanonicalCacheKey) -> CacheHit | None:
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(cache_entries, artifact_blobs)
                        .join(
                            artifact_blobs,
                            cache_entries.c.blob_sha256 == artifact_blobs.c.content_sha256,
                        )
                        .where(cache_entries.c.cache_key == key.sha256)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["state"] != "ready":
            return None
        if str(row["canonical_payload_json"]) != key.canonical_payload_json:
            raise PipelineError(
                ErrorCode.CACHE_KEY_COLLISION,
                "cache",
                "cache key maps to different canonical request data",
                retryable=False,
            )
        try:
            blob = self._blob_from_row(dict(row))
        except (OSError, PipelineError):
            await self._invalidate(key.sha256, reason="blob_missing_or_corrupt")
            return None
        async with self._database.write_session() as session:
            await session.execute(
                update(cache_entries)
                .where(cache_entries.c.cache_key == key.sha256)
                .where(cache_entries.c.state == "ready")
                .values(
                    last_hit_at_utc=_now(),
                    hit_count=cache_entries.c.hit_count + 1,
                )
            )
        return CacheHit(blob)

    async def put(self, key: CanonicalCacheKey, blob: PublishedBlob) -> None:
        self._verify_blob(blob)
        now = _now()
        async with self._database.write_session() as session:
            await session.execute(
                sqlite_insert(artifact_blobs)
                .values(
                    content_sha256=blob.content_sha256,
                    relative_path=blob.relative_path.as_posix(),
                    byte_size=blob.byte_size,
                    frames=blob.audio.frames,
                    sample_rate=blob.audio.sample_rate,
                    channels=blob.audio.channels,
                    duration_seconds=blob.audio.duration_seconds,
                    rms_dbfs=blob.audio.rms_dbfs,
                    peak_dbfs=blob.audio.peak_dbfs,
                    lifecycle_state="ready",
                    created_at_utc=now,
                    checked_at_utc=now,
                )
                .on_conflict_do_nothing(index_elements=[artifact_blobs.c.content_sha256])
            )
            existing = (
                await session.execute(
                    select(cache_entries.c.canonical_payload_json).where(
                        cache_entries.c.cache_key == key.sha256
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if str(existing) != key.canonical_payload_json:
                    raise PipelineError(
                        ErrorCode.CACHE_KEY_COLLISION,
                        "cache",
                        "cache key maps to different canonical request data",
                        retryable=False,
                    )
                return
            await session.execute(
                insert(cache_entries).values(
                    cache_key=key.sha256,
                    kind=key.kind,
                    canonical_payload_json=key.canonical_payload_json,
                    blob_sha256=blob.content_sha256,
                    source_version_id=None,
                    state="ready",
                    created_at_utc=now,
                    last_hit_at_utc=now,
                    hit_count=0,
                )
            )

    async def inspect(self) -> list[dict[str, Any]]:
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(cache_entries).order_by(cache_entries.c.created_at_utc)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def prune(
        self,
        *,
        max_entries_per_kind: int,
        max_age_days: int,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Invalidate expired and least-recently-used cache keys deterministically."""
        if max_entries_per_kind < 1 or max_age_days < 1:
            raise ValueError("cache retention limits must be positive")
        observed_at = now or datetime.now(UTC)
        cutoff = observed_at - timedelta(days=max_age_days)
        async with self._database.read_session() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            cache_entries.c.cache_key,
                            cache_entries.c.kind,
                            cache_entries.c.last_hit_at_utc,
                        )
                        .where(cache_entries.c.state == "ready")
                        .order_by(
                            cache_entries.c.kind,
                            cache_entries.c.last_hit_at_utc.desc(),
                            cache_entries.c.cache_key.desc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
        keep_by_kind: dict[str, int] = {}
        expired_or_lru: list[str] = []
        for row in rows:
            key = str(row["cache_key"])
            kind = str(row["kind"])
            last_hit = datetime.fromisoformat(str(row["last_hit_at_utc"]))
            if last_hit < cutoff:
                expired_or_lru.append(key)
                continue
            retained = keep_by_kind.get(kind, 0)
            if retained >= max_entries_per_kind:
                expired_or_lru.append(key)
                continue
            keep_by_kind[kind] = retained + 1
        if not expired_or_lru:
            return ()
        async with self._database.write_session() as session:
            await session.execute(
                update(cache_entries)
                .where(cache_entries.c.cache_key.in_(expired_or_lru))
                .where(cache_entries.c.state == "ready")
                .values(state="invalid")
            )
        return tuple(expired_or_lru)

    async def _invalidate(self, cache_key: str, *, reason: str) -> None:
        async with self._database.write_session() as session:
            await session.execute(
                update(cache_entries)
                .where(cache_entries.c.cache_key == cache_key)
                .values(state="invalid")
            )

    def _blob_from_row(self, row: dict[str, Any]) -> PublishedBlob:
        path = (self._artifacts.root / str(row["relative_path"])).resolve()
        if (
            not path.is_file()
            or path.is_symlink()
            or sha256_file(path) != str(row["content_sha256"])
        ):
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "cache",
                "cached blob is missing or corrupt",
                retryable=False,
            )
        audio = probe_wav(path, require_reference_window=False)
        return PublishedBlob(
            content_sha256=str(row["content_sha256"]),
            relative_path=path.relative_to(self._artifacts.root),
            absolute_path=path,
            byte_size=path.stat().st_size,
            audio=audio,
            reused_existing=True,
        )

    def _verify_blob(self, blob: PublishedBlob) -> None:
        path = blob.absolute_path
        if not path.is_file() or path.is_symlink() or sha256_file(path) != blob.content_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT, "cache", "cannot cache an invalid blob", retryable=False
            )
        probe_wav(path, require_reference_window=False)


def _now() -> str:
    return datetime.now(UTC).isoformat()
