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
from voice_pipeline.models.persistence import RetryJobRequest
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
        }
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
            "cancel_requested_at": (
                record.cancel_requested_at_utc.isoformat()
                if record.cancel_requested_at_utc is not None
                else None
            ),
            "request_snapshot": record.request_snapshot,
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
    context = await plane.registry.create(
        request_id=request.request_id,
        kind=kind,
        request_snapshot=request.model_dump(mode="json"),
    )

    await _require_dispatcher(plane).notify()
    return {
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "status": "queued",
        "status_url": f"/api/v1/jobs/{context.job_id}",
    }


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
