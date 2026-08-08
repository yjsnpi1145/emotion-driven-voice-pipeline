from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.modules.cache.keys import CanonicalCacheKey
from voice_pipeline.modules.quality.models import QualityReport
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import quality_cache_entries


class QualityCacheStore:
    """Persistent VAD/ASR report cache keyed by verified reference input."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_valid(self, key: CanonicalCacheKey) -> QualityReport | None:
        expected = key.payload
        async with self._database.read_session() as session:
            row = (
                (
                    await session.execute(
                        select(quality_cache_entries).where(
                            quality_cache_entries.c.cache_key == key.sha256
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["state"] != "ready":
            return None
        if (
            str(row["audio_sha256"]) != expected["audio_sha256"]
            or str(row["expected_text_sha256"]) != expected["expected_text_sha256"]
            or str(row["policy_fingerprint_sha256"]) != expected["policy_fingerprint"]
        ):
            raise PipelineError(
                ErrorCode.CACHE_KEY_COLLISION,
                "quality_cache",
                "quality cache key maps to different verified input",
                retryable=False,
            )
        try:
            report = QualityReport.model_validate(json.loads(str(row["report_json"])))
        except (ValueError, TypeError):
            await self._invalidate(key.sha256)
            return None
        if report.policy_fingerprint != expected["policy_fingerprint"]:
            await self._invalidate(key.sha256)
            return None
        async with self._database.write_session() as session:
            await session.execute(
                update(quality_cache_entries)
                .where(quality_cache_entries.c.cache_key == key.sha256)
                .where(quality_cache_entries.c.state == "ready")
                .values(last_hit_at_utc=_now())
            )
        return report

    async def put(self, key: CanonicalCacheKey, report: QualityReport) -> None:
        payload = key.payload
        if report.policy_fingerprint != payload["policy_fingerprint"]:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "quality_cache",
                "quality report does not match its configured policy fingerprint",
                retryable=False,
            )
        now = _now()
        async with self._database.write_session() as session:
            existing = (
                await session.execute(
                    select(
                        quality_cache_entries.c.audio_sha256,
                        quality_cache_entries.c.expected_text_sha256,
                        quality_cache_entries.c.policy_fingerprint_sha256,
                    ).where(quality_cache_entries.c.cache_key == key.sha256)
                )
            ).one_or_none()
            if existing is not None:
                if tuple(map(str, existing)) != (
                    str(payload["audio_sha256"]),
                    str(payload["expected_text_sha256"]),
                    str(payload["policy_fingerprint"]),
                ):
                    raise PipelineError(
                        ErrorCode.CACHE_KEY_COLLISION,
                        "quality_cache",
                        "quality cache key maps to different verified input",
                        retryable=False,
                    )
                return
            await session.execute(
                sqlite_insert(quality_cache_entries).values(
                    cache_key=key.sha256,
                    audio_sha256=str(payload["audio_sha256"]),
                    expected_text_sha256=str(payload["expected_text_sha256"]),
                    policy_fingerprint_sha256=str(payload["policy_fingerprint"]),
                    report_json=json.dumps(
                        report.model_dump(mode="json"),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    state="ready",
                    created_at_utc=now,
                    last_hit_at_utc=now,
                )
            )

    async def _invalidate(self, cache_key: str) -> None:
        async with self._database.write_session() as session:
            await session.execute(
                update(quality_cache_entries)
                .where(quality_cache_entries.c.cache_key == cache_key)
                .values(state="invalid")
            )


def _now() -> str:
    return datetime.now(UTC).isoformat()
