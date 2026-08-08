from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import SegmentJobSnapshot
from voice_pipeline.models.schemas import StrictModel
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.orm import (
    artifact_blobs,
    artifact_version_state,
    artifact_versions,
    cache_entries,
    generation_jobs,
    retention_candidates,
    retention_plans,
    segments,
    storage_meta,
)


class RetentionPlan(StrictModel):
    plan_id: UUID
    storage_revision: int
    candidate_version_ids: tuple[UUID, ...]


class RetentionReceipt(StrictModel):
    plan_id: UUID
    status: str
    deleted_version_ids: tuple[UUID, ...]


class RetentionPlanner:
    def __init__(self, database: Database, *, history_limit: int = 5) -> None:
        if history_limit != 5:
            raise ValueError("history_limit must be exactly 5")
        self._database = database
        self._history_limit = history_limit

    async def plan(self, *, segment_id: UUID | None = None) -> RetentionPlan:
        async with self._database.write_session() as session:
            revision = int(
                (
                    await session.execute(
                        select(storage_meta.c.protected_graph_revision).where(
                            storage_meta.c.singleton_id == 1
                        )
                    )
                ).scalar_one()
            )
            statement = (
                select(
                    artifact_versions,
                    artifact_version_state.c.state,
                    segments.c.active_ref_version_id,
                    segments.c.active_gsv_version_id,
                )
                .join(
                    artifact_version_state,
                    artifact_versions.c.version_id == artifact_version_state.c.version_id,
                )
                .join(segments, artifact_versions.c.segment_id == segments.c.segment_id)
                .where(artifact_version_state.c.state == "ready")
            )
            if segment_id is not None:
                statement = statement.where(artifact_versions.c.segment_id == str(segment_id))
            rows = (await session.execute(statement)).mappings().all()
            protected = await _inflight_version_ids(session)
            candidates = _find_candidates(
                [dict(row) for row in rows],
                protected=protected,
                history_limit=self._history_limit,
            )
            plan_id = uuid4()
            now = _now()
            await session.execute(
                insert(retention_plans).values(
                    plan_id=str(plan_id),
                    storage_revision=revision,
                    status="planned",
                    scope_json=json.dumps(
                        {"segment_id": str(segment_id) if segment_id else None},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    summary_json=json.dumps(
                        {"candidate_count": len(candidates)}, sort_keys=True, separators=(",", ":")
                    ),
                    created_at_utc=now,
                    applied_at_utc=None,
                )
            )
            for ordinal, row in enumerate(candidates):
                await session.execute(
                    insert(retention_candidates).values(
                        plan_id=str(plan_id),
                        version_id=str(row["version_id"]),
                        artifact_type=row["artifact_type"],
                        reason="outside_current_and_history_quota",
                        action="delete",
                        protection_reason=None,
                        ordinal=ordinal,
                    )
                )
        return RetentionPlan(
            plan_id=plan_id,
            storage_revision=revision,
            candidate_version_ids=tuple(UUID(str(row["version_id"])) for row in candidates),
        )


class RetentionExecutor:
    def __init__(self, database: Database, artifacts: ArtifactStore) -> None:
        self._database = database
        self._artifacts = artifacts

    async def apply(self, plan_id: UUID) -> RetentionReceipt:
        async with self._database.write_session() as session:
            plan = (
                (
                    await session.execute(
                        select(retention_plans).where(retention_plans.c.plan_id == str(plan_id))
                    )
                )
                .mappings()
                .one_or_none()
            )
            if plan is None:
                raise KeyError(f"unknown retention plan: {plan_id}")
            candidates = (
                (
                    await session.execute(
                        select(retention_candidates.c.version_id)
                        .where(retention_candidates.c.plan_id == str(plan_id))
                        .order_by(retention_candidates.c.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            if str(plan["status"]) == "applied":
                return RetentionReceipt(
                    plan_id=plan_id,
                    status="applied",
                    deleted_version_ids=tuple(UUID(str(value)) for value in candidates),
                )
            revision = int(
                (
                    await session.execute(
                        select(storage_meta.c.protected_graph_revision).where(
                            storage_meta.c.singleton_id == 1
                        )
                    )
                ).scalar_one()
            )
            if revision != int(plan["storage_revision"]):
                raise PipelineError(
                    ErrorCode.RETENTION_PLAN_STALE,
                    "retention",
                    "protected graph changed after this retention plan was created",
                    retryable=False,
                )
            for version_id in candidates:
                await session.execute(
                    update(artifact_version_state)
                    .where(artifact_version_state.c.version_id == str(version_id))
                    .where(artifact_version_state.c.state == "ready")
                    .values(state="deleting", checked_at_utc=_now())
                )
            for version_id in candidates:
                await session.execute(
                    update(artifact_version_state)
                    .where(artifact_version_state.c.version_id == str(version_id))
                    .where(artifact_version_state.c.state == "deleting")
                    .values(state="deleted", checked_at_utc=_now())
                )
            await session.execute(
                update(retention_plans)
                .where(retention_plans.c.plan_id == str(plan_id))
                .values(status="applied", applied_at_utc=_now())
            )
        await self._garbage_collect_blobs()
        return RetentionReceipt(
            plan_id=plan_id,
            status="applied",
            deleted_version_ids=tuple(UUID(str(value)) for value in candidates),
        )

    async def _garbage_collect_blobs(self) -> None:
        async with self._database.read_session() as session:
            rows = (
                await session.execute(
                    select(artifact_blobs.c.content_sha256, artifact_blobs.c.relative_path)
                )
            ).all()
            live_versions = set(
                (
                    await session.execute(
                        select(artifact_versions.c.blob_sha256)
                        .join(
                            artifact_version_state,
                            artifact_versions.c.version_id == artifact_version_state.c.version_id,
                        )
                        .where(artifact_version_state.c.state.in_(("ready", "deleting")))
                    )
                ).scalars()
            )
            cached = set(
                (
                    await session.execute(
                        select(cache_entries.c.blob_sha256).where(cache_entries.c.state == "ready")
                    )
                ).scalars()
            )
        for sha, relative in rows:
            if str(sha) in live_versions or str(sha) in cached:
                continue
            path = (self._artifacts.root / Path(str(relative))).resolve()
            try:
                path.relative_to((self._artifacts.root / "blobs").resolve())
            except ValueError:
                continue
            if path.is_file() and not path.is_symlink():
                trash = self._artifacts.root / "trash" / "retention" / path.name
                trash.parent.mkdir(parents=True, exist_ok=True)
                if not trash.exists():
                    path.replace(trash)


def _find_candidates(
    rows: list[dict[str, Any]], *, protected: set[str], history_limit: int
) -> list[dict[str, Any]]:
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    current: set[str] = set()
    for row in rows:
        by_group[(str(row["segment_id"]), str(row["artifact_type"]))].append(row)
        current.update(
            value
            for value in (row["active_ref_version_id"], row["active_gsv_version_id"])
            if value is not None
        )
    keep: set[str] = set(current) | protected
    for group_rows in by_group.values():
        ordered = sorted(
            group_rows,
            key=lambda item: (str(item["created_at_utc"]), str(item["version_id"])),
            reverse=True,
        )
        non_current = [row for row in ordered if str(row["version_id"]) not in current]
        keep.update(str(row["version_id"]) for row in non_current[:history_limit])
    for row in rows:
        if str(row["artifact_type"]) == "gsv" and str(row["version_id"]) in keep:
            if row["ref_version_id"] is not None:
                keep.add(str(row["ref_version_id"]))
    candidates = [row for row in rows if str(row["version_id"]) not in keep]
    return sorted(
        candidates,
        key=lambda row: (0 if row["artifact_type"] == "gsv" else 1, str(row["created_at_utc"])),
    )


async def _inflight_version_ids(session: Any) -> set[str]:
    raw_snapshots = (
        await session.execute(
            select(generation_jobs.c.segment_snapshot_json).where(
                generation_jobs.c.status.in_(("queued", "running"))
            )
        )
    ).scalars()
    protected: set[str] = set()
    for raw in raw_snapshots:
        if raw is None:
            continue
        try:
            snapshot = SegmentJobSnapshot.model_validate_json(str(raw))
        except ValueError:
            continue
        for version_id in (snapshot.active_ref_version_id, snapshot.active_gsv_version_id):
            if version_id is not None:
                protected.add(str(version_id))
    return protected


def _now() -> str:
    return datetime.now(UTC).isoformat()
