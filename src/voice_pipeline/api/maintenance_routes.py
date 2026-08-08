from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException

from voice_pipeline.core.errors import PipelineError


def build_maintenance_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/maintenance/retention/plan", status_code=201)
    async def plan_retention(segment_id: UUID | None = None) -> dict[str, Any]:
        try:
            plan = await _planner(plane).plan(segment_id=segment_id)
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return cast(dict[str, Any], plan.model_dump(mode="json"))

    @router.post("/api/v1/maintenance/retention/{plan_id}/apply")
    async def apply_retention(plan_id: UUID) -> dict[str, Any]:
        try:
            receipt = await _executor(plane).apply(plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="retention plan not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return cast(dict[str, Any], receipt.model_dump(mode="json"))

    return router


def _planner(plane: Any) -> Any:
    planner = getattr(plane, "retention_planner", None)
    if planner is None:
        raise HTTPException(status_code=503, detail="retention planner is not ready")
    return planner


def _executor(plane: Any) -> Any:
    executor = getattr(plane, "retention_executor", None)
    if executor is None:
        raise HTTPException(status_code=503, detail="retention executor is not ready")
    return executor
