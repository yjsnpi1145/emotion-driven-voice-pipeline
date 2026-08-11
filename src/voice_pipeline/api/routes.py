from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.model_profiles import ModelProfileSnapshot
from voice_pipeline.models.persistence import GsvModelSnapshot, RetryJobRequest
from voice_pipeline.models.schemas import (
    GsvJobRequest,
    ReferenceJobRequest,
    SegmentSynthesisRequest,
)


def build_router(plane: Any) -> APIRouter:
    router = APIRouter()

    # ------------------------------------------------------------------ #
    # health
    # ------------------------------------------------------------------ #

    @router.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        runtime_health = plane.runtime.health()
        queue_stats = plane.queue.stats()
        workers: dict[str, Any] = {}
        for engine in ("indextts", "gpt_sovits"):
            worker = getattr(runtime_health.workers, engine)
            workers[engine] = worker.model_dump(mode="json")
        quality = getattr(plane, "quality_analyzer", None)
        quality_health = {
            "mode": plane.settings.quality.mode,
            "status": "ready" if quality is not None else "unavailable",
            "policy_fingerprint_sha256": quality.policy_fingerprint
            if quality is not None
            else None,
            "asr_text_scoring_enabled": (
                quality.asr_text_scoring_enabled
                if quality is not None and hasattr(quality, "asr_text_scoring_enabled")
                else True
            ),
        }
        storage_health = await _storage_health(plane)
        job_counts = await _job_counts(plane)
        dispatcher = getattr(plane, "dispatcher", None)
        dispatcher_stats = dispatcher.stats() if dispatcher is not None else None
        return {
            "status": _overall_status(runtime_health, plane.settings.engine_lifecycle, queue_stats),
            "mode": plane.settings.mode,
            "engine_lifecycle": plane.settings.engine_lifecycle,
            "control": {
                "pid": os.getpid(),
                "instance_id": str(plane.audit.instance_id),
                "python_executable": sys.executable,
                "audit_log": str(plane.audit.log_path),
            },
            "workers": workers,
            "quality": quality_health,
            "storage": storage_health,
            "dispatcher": {
                "state": dispatcher_stats.state if dispatcher_stats is not None else "stopped",
                "queued_count": (
                    dispatcher_stats.queued_count if dispatcher_stats is not None else 0
                ),
                "active_job_id": (
                    str(dispatcher_stats.active_job_id)
                    if dispatcher_stats is not None and dispatcher_stats.active_job_id is not None
                    else None
                ),
                "recovered_interrupted_count": (
                    dispatcher_stats.recovered_interrupted_count
                    if dispatcher_stats is not None
                    else 0
                ),
            },
            "job_counts": job_counts,
            "gpu_queue": {
                "state": queue_stats.state,
                "poison_reason": queue_stats.poison_reason,
                "active_count": queue_stats.active_count,
                "queued_count": queue_stats.queued_count,
                "max_active_observed": queue_stats.max_active_observed,
                "max_concurrency": queue_stats.max_concurrency,
            },
        }

    # ------------------------------------------------------------------ #
    # job submission
    # ------------------------------------------------------------------ #

    @router.post("/api/v1/jobs/reference", status_code=202)
    async def submit_reference(request: ReferenceJobRequest) -> dict[str, Any]:
        return await _submit(plane, request, "reference")

    @router.post("/api/v1/jobs/gsv", status_code=202)
    async def submit_gsv(request: GsvJobRequest) -> dict[str, Any]:
        return await _submit(plane, request, "gsv")

    @router.post("/api/v1/jobs/segment", status_code=202)
    async def submit_segment(request: SegmentSynthesisRequest) -> dict[str, Any]:
        return await _submit(plane, request, "segment")

    # ------------------------------------------------------------------ #
    # job status
    # ------------------------------------------------------------------ #

    @router.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: UUID) -> dict[str, Any]:
        record = await _require_job(plane, job_id)
        audio_urls, manifest_urls = _urls_for(record)
        return {
            "job_id": str(record.job_id),
            "request_id": str(record.request_id),
            "kind": record.kind,
            "status": record.status,
            "stage": record.stage,
            "created_at": record.created_at.isoformat(),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "finished_at": (record.finished_at.isoformat() if record.finished_at else None),
            "retry_of_job_id": (
                str(record.retry_of_job_id) if record.retry_of_job_id is not None else None
            ),
            "attempt": record.attempt,
            "activation_outcome": record.activation_outcome,
            "cancel_requested_at": (
                record.cancel_requested_at_utc.isoformat()
                if record.cancel_requested_at_utc is not None
                else None
            ),
            "request_snapshot": record.request_snapshot,
            "model_profile_snapshot": (
                record.model_profile_snapshot.model_dump(mode="json")
                if record.model_profile_snapshot is not None
                else None
            ),
            "output_spec": (
                record.output_spec.model_dump(mode="json")
                if record.output_spec is not None
                else None
            ),
            "result": record.result,
            "audio_urls": audio_urls,
            "manifest_urls": manifest_urls,
            "error": record.error,
        }

    @router.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: UUID) -> JSONResponse:
        dispatcher = _require_dispatcher(plane)
        try:
            record = await dispatcher.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return JSONResponse(
            status_code=200 if record.status == "cancelled" else 202,
            content={
                "job_id": str(record.job_id),
                "status": record.status,
                "cancellation_requested": record.cancel_requested_at_utc is not None,
            },
        )

    @router.post("/api/v1/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: UUID, request: RetryJobRequest) -> dict[str, Any]:
        del request  # schema fixes the only supported retry mode for this batch.
        dispatcher = _require_dispatcher(plane)
        try:
            context = await plane.registry.clone_for_retry(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        await dispatcher.notify()
        return {
            "job_id": str(context.job_id),
            "request_id": str(context.request_id),
            "status": "queued",
            "status_url": f"/api/v1/jobs/{context.job_id}",
        }

    # ------------------------------------------------------------------ #
    # audio
    # ------------------------------------------------------------------ #

    @router.get("/api/v1/jobs/{job_id}/audio/reference")
    async def audio_reference(job_id: UUID) -> Any:
        return await _audio(plane, job_id, "reference")

    @router.get("/api/v1/jobs/{job_id}/audio/target")
    async def audio_target(job_id: UUID) -> Any:
        return await _audio(plane, job_id, "target")

    # ------------------------------------------------------------------ #
    # manifests
    # ------------------------------------------------------------------ #

    @router.get("/api/v1/jobs/{job_id}/manifest/reference")
    async def manifest_reference(job_id: UUID) -> Any:
        return await _manifest(plane, job_id, "reference")

    @router.get("/api/v1/jobs/{job_id}/manifest/run")
    async def manifest_run(job_id: UUID) -> Any:
        return await _manifest(plane, job_id, "run")

    # ------------------------------------------------------------------ #
    # control
    # ------------------------------------------------------------------ #

    @router.post("/api/v1/control/shutdown")
    async def shutdown(request: Request) -> dict[str, str]:
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="loopback only")
        await plane.shutdown()
        return {"status": "shutting_down"}

    return router


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


async def _storage_health(plane: Any) -> dict[str, Any]:
    database = getattr(plane, "database", None)
    store = getattr(plane, "artifact_store", None)
    recovery = getattr(plane, "last_recovery_report", None)
    if database is None or store is None or recovery is None:
        return {
            "status": "unavailable",
            "database_path": None,
            "alembic_revision": None,
            "journal_mode": None,
            "quick_check": None,
            "artifact_root": None,
            "missing_ready_versions": 0,
            "corrupt_ready_versions": 0,
            "last_recovery_run_id": None,
        }
    return {
        "status": "ready",
        "database_path": str(plane.settings.storage.database_path),
        "alembic_revision": await database.alembic_revision(),
        "journal_mode": (await database.scalar_text("PRAGMA journal_mode")).casefold(),
        "quick_check": await database.quick_check_text(),
        "artifact_root": str(store.root),
        "missing_ready_versions": len(recovery.missing_versions),
        "corrupt_ready_versions": len(recovery.corrupt_versions),
        "last_recovery_run_id": str(recovery.recovery_run_id),
    }


async def _job_counts(plane: Any) -> dict[str, int]:
    registry = getattr(plane, "registry", None)
    counts_method = getattr(registry, "status_counts", None)
    if counts_method is None:
        return {"queued": 0, "running": 0, "interrupted": 0}
    raw: object = await counts_method()
    if not isinstance(raw, dict):  # pragma: no cover - durable store invariant
        return {"queued": 0, "running": 0, "interrupted": 0}
    return {name: int(raw.get(name, 0)) for name in ("queued", "running", "interrupted")}


def _overall_status(runtime_health: Any, lifecycle: str, queue_stats: Any) -> str:
    workers = runtime_health.workers
    if queue_stats.state == "poisoned":
        return "degraded"
    states = [workers.indextts.state, workers.gpt_sovits.state]
    if any(state in ("unknown", "unhealthy") for state in states):
        return "degraded"
    if lifecycle == "resident":
        return "ready" if all(state == "ready" for state in states) else "degraded"
    # exclusive_process: zero or one ready, the other stopped_expected
    ready = sum(1 for state in states if state == "ready")
    stopped = sum(1 for state in states if state == "stopped_expected")
    if ready <= 1 and ready + stopped == 2:
        return "ready"
    return "degraded"


async def _submit(plane: Any, request: Any, kind: str) -> dict[str, Any]:
    if not plane.accepting:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": ErrorCode.ENGINE_UNAVAILABLE.value,
                    "stage": "api",
                    "message": "control plane is not accepting new jobs",
                    "retryable": False,
                    "details": {},
                }
            },
        )
    frozen_request, model_profile_snapshot = await _freeze_submission_model_profile(
        plane, request, kind
    )
    context = await plane.registry.create(
        request_id=frozen_request.request_id,
        kind=kind,
        request_snapshot=frozen_request.model_dump(mode="json"),
        model_fingerprint=(
            plane.gsv.fingerprint().model_dump(mode="json")
            if kind in {"gsv", "segment"}
            else plane.index.fingerprint().model_dump(mode="json")
        ),
        model_profile_snapshot=model_profile_snapshot,
    )

    await _require_dispatcher(plane).notify()
    return {
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "status": "queued",
        "status_url": f"/api/v1/jobs/{context.job_id}",
    }


async def _freeze_submission_model_profile(
    plane: Any, request: Any, kind: str
) -> tuple[Any, GsvModelSnapshot | None]:
    """Resolve the selected GSV profile once, before a durable job is queued."""
    if kind not in {"gsv", "segment"}:
        return request, None
    if not isinstance(request, (GsvJobRequest, SegmentSynthesisRequest)):
        raise TypeError(f"{kind} submission has an unexpected request schema")
    model_profiles = getattr(plane, "model_profiles", None)
    if model_profiles is None:
        return request, None
    try:
        resolved = await model_profiles.resolve_selected_profile(request.model_profile_id)
    except PipelineError as exc:
        if (
            plane.settings.mode != "real"
            and request.model_profile_id is None
            and exc.code == ErrorCode.MODEL_PROFILE_UNAVAILABLE
            and exc.details.get("reason") == "no_active_profile"
        ):
            return request, None
        raise
    profile = ModelProfileSnapshot(
        profile_id=resolved.profile_id,
        display_name=resolved.display_name,
        gpt_relative_path=resolved.gpt_relative_path,
        sovits_relative_path=resolved.sovits_relative_path,
        gpt_sha256=resolved.gpt_sha256,
        sovits_sha256=resolved.sovits_sha256,
    )
    return (
        request.model_copy(update={"model_profile_id": profile.profile_id}),
        GsvModelSnapshot(profile=profile, engine_fingerprint=plane.gsv.fingerprint()),
    )


def _require_dispatcher(plane: Any) -> Any:
    dispatcher = getattr(plane, "dispatcher", None)
    if dispatcher is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": ErrorCode.ENGINE_UNAVAILABLE.value,
                    "stage": "dispatcher",
                    "message": "durable job dispatcher is not ready",
                    "retryable": True,
                    "details": {},
                }
            },
        )
    return dispatcher


async def _require_job(plane: Any, job_id: UUID) -> Any:
    try:
        return await plane.registry.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


def _urls_for(record: Any) -> tuple[list[str], list[str]]:
    base = f"/api/v1/jobs/{record.job_id}"
    if record.status != "succeeded":
        return [], []
    if record.kind == "reference":
        return [f"{base}/audio/reference"], [f"{base}/manifest/reference"]
    if record.kind == "gsv":
        return [f"{base}/audio/target"], [f"{base}/manifest/run"]
    if record.kind == "segment":
        return (
            [f"{base}/audio/reference", f"{base}/audio/target"],
            [f"{base}/manifest/reference", f"{base}/manifest/run"],
        )
    return [], []


def _audio_path(record: Any, which: str) -> Path | None:
    result = record.result or {}
    if record.kind == "reference":
        audio = (result.get("reference") or {}).get("audio") or {}
        return Path(audio["path"]) if which == "reference" and audio.get("path") else None
    if record.kind == "gsv":
        target = result.get("target") or {}
        return Path(target["path"]) if which == "target" and target.get("path") else None
    if record.kind == "segment":
        audio = result.get(which) or {}
        return Path(audio["path"]) if audio.get("path") else None
    return None


def _manifest_path(record: Any, which: str) -> Path | None:
    result = record.result or {}
    if record.kind == "reference":
        path = result.get("manifest_path")
        return Path(path) if which == "reference" and path else None
    if record.kind == "gsv":
        path = result.get("manifest_path")
        return Path(path) if which == "run" and path else None
    if record.kind == "segment":
        key = "reference_manifest_path" if which == "reference" else "run_manifest_path"
        path = result.get(key)
        return Path(path) if path else None
    return None


async def _audio(plane: Any, job_id: UUID, which: str) -> Any:
    record = await _require_job(plane, job_id)
    if record.status == "failed":
        raise HTTPException(status_code=409, detail={"error": record.error})
    if record.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "JOB_NOT_FINISHED",
                    "stage": "api",
                    "message": f"job is {record.status}",
                    "retryable": False,
                    "details": {},
                }
            },
        )
    path = _audio_path(record, which)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="audio not available")
    return FileResponse(path, media_type="audio/wav")


async def _manifest(plane: Any, job_id: UUID, which: str) -> Any:
    record = await _require_job(plane, job_id)
    if record.status == "failed":
        raise HTTPException(status_code=409, detail={"error": record.error})
    if record.status != "succeeded":
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "JOB_NOT_FINISHED",
                    "stage": "api",
                    "message": f"job is {record.status}",
                    "retryable": False,
                    "details": {},
                }
            },
        )
    path = _manifest_path(record, which)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="manifest not available")
    payload = json.loads(await asyncio.to_thread(Path(path).read_text, "utf-8"))
    return JSONResponse(payload)
