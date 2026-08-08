from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import (
    CreateDubbingTaskRequest,
    CreateSegmentRequest,
    SegmentInputsPatch,
)


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

    return router


def _store(plane: Any) -> Any:
    store = getattr(plane, "segment_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="segment store is not ready")
    return store


def _error_response(exc: PipelineError, *, invalid_status: int) -> HTTPException:
    status = invalid_status if exc.code == ErrorCode.INVALID_INPUT else 409
    return HTTPException(status_code=status, detail={"error": exc.as_dict()})
