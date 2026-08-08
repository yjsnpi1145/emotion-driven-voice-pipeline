from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import (
    ActivateVersionRequest,
    CreateDubbingTaskRequest,
    CreateSegmentRequest,
    SegmentGsvJobRequest,
    SegmentInputsPatch,
    SegmentReferenceJobRequest,
)
from voice_pipeline.modules.audio.wav_probe import sha256_file


def build_foundation_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/tasks", status_code=201)
    async def create_task(request: CreateDubbingTaskRequest) -> dict[str, Any]:
        return cast(
            dict[str, Any], (await _store(plane).create_task(request)).model_dump(mode="json")
        )

    @router.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: UUID) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any], (await _store(plane).get_task(task_id)).model_dump(mode="json")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @router.post("/api/v1/tasks/{task_id}/segments", status_code=201)
    async def create_segment(task_id: UUID, request: CreateSegmentRequest) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                (await _store(plane).create_segment(task_id, request)).model_dump(mode="json"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except PipelineError as exc:
            raise _error_response(exc, invalid_status=422) from exc

    @router.get("/api/v1/tasks/{task_id}/segments")
    async def list_segments(task_id: UUID) -> list[dict[str, Any]]:
        try:
            return cast(
                list[dict[str, Any]],
                [
                    record.model_dump(mode="json")
                    for record in await _store(plane).list_segments(task_id)
                ],
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @router.get("/api/v1/segments/{segment_id}")
    async def get_segment(segment_id: UUID) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                (await _store(plane).get_segment(segment_id)).model_dump(mode="json"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment not found") from exc

    @router.patch("/api/v1/segments/{segment_id}/inputs")
    async def patch_segment(segment_id: UUID, request: SegmentInputsPatch) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                (await _store(plane).patch_inputs(segment_id, request)).model_dump(mode="json"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment not found") from exc
        except PipelineError as exc:
            raise _error_response(exc, invalid_status=422) from exc

    @router.post("/api/v1/segments/{segment_id}/jobs/reference", status_code=202)
    async def submit_segment_reference(
        segment_id: UUID, request: SegmentReferenceJobRequest
    ) -> dict[str, str]:
        try:
            context = await _segment_jobs(plane).submit_reference(segment_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment not found") from exc
        except PipelineError as exc:
            raise _error_response(exc, invalid_status=422) from exc
        await _dispatcher(plane).notify()
        return _submitted(context.job_id, context.request_id)

    @router.post("/api/v1/segments/{segment_id}/jobs/gsv", status_code=202)
    async def submit_segment_gsv(segment_id: UUID, request: SegmentGsvJobRequest) -> dict[str, str]:
        try:
            context = await _segment_jobs(plane).submit_gsv(segment_id, request)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="segment or reference version not found"
            ) from exc
        except PipelineError as exc:
            raise _error_response(exc, invalid_status=422) from exc
        await _dispatcher(plane).notify()
        return _submitted(context.job_id, context.request_id)

    @router.get("/api/v1/segments/{segment_id}/versions")
    async def list_versions(segment_id: UUID) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            [
                record.model_dump(mode="json")
                for record in await _versions(plane).list_versions(segment_id)
            ],
        )

    @router.post("/api/v1/segments/{segment_id}/versions/{version_id}/activate")
    async def activate_version(
        segment_id: UUID, version_id: UUID, request: ActivateVersionRequest
    ) -> dict[str, str | None]:
        try:
            reference_id, gsv_id = await _versions(plane).activate(segment_id, version_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment or version not found") from exc
        except PipelineError as exc:
            raise _error_response(exc, invalid_status=422) from exc
        return {
            "active_ref_version_id": str(reference_id) if reference_id else None,
            "active_gsv_version_id": str(gsv_id) if gsv_id else None,
        }

    @router.get("/api/v1/versions/{version_id}")
    async def get_version(version_id: UUID) -> dict[str, Any]:
        try:
            return cast(
                dict[str, Any],
                (await _versions(plane).get_version(version_id)).model_dump(mode="json"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="version not found") from exc

    @router.get("/api/v1/versions/{version_id}/audio")
    async def get_version_audio(version_id: UUID) -> FileResponse:
        try:
            version = await _versions(plane).get_version(version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="version not found") from exc
        if version.state != "ready":
            raise HTTPException(status_code=409, detail="version audio is not ready")
        path = (plane.artifact_store.root / version.blob_relative_path).resolve()
        try:
            path.relative_to((plane.artifact_store.root / "blobs").resolve())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="version blob path is invalid") from exc
        if not path.is_file() or path.is_symlink() or sha256_file(path) != version.blob_sha256:
            raise HTTPException(status_code=409, detail="version blob is missing or corrupt")
        return FileResponse(path, media_type="audio/wav")

    return router


def _store(plane: Any) -> Any:
    store = getattr(plane, "segment_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="segment store is not ready")
    return store


def _versions(plane: Any) -> Any:
    store = getattr(plane, "version_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="version store is not ready")
    return store


def _segment_jobs(plane: Any) -> Any:
    service = getattr(plane, "segment_jobs", None)
    if service is None:
        raise HTTPException(status_code=503, detail="segment job service is not ready")
    return service


def _dispatcher(plane: Any) -> Any:
    dispatcher = getattr(plane, "dispatcher", None)
    if dispatcher is None:
        raise HTTPException(status_code=503, detail="durable dispatcher is not ready")
    return dispatcher


def _submitted(job_id: UUID, request_id: UUID) -> dict[str, str]:
    return {
        "job_id": str(job_id),
        "request_id": str(request_id),
        "status": "queued",
        "status_url": f"/api/v1/jobs/{job_id}",
    }


def _error_response(exc: PipelineError, *, invalid_status: int) -> HTTPException:
    status = invalid_status if exc.code == ErrorCode.INVALID_INPUT else 409
    return HTTPException(status_code=status, detail={"error": exc.as_dict()})
