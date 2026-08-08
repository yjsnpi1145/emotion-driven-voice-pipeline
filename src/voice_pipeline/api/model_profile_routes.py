from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.model_profile_service import ModelProfileService
from voice_pipeline.models.model_profiles import ImportModelProfileRequest, ModelProfileView


def build_model_profile_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/model-profiles/import", response_model=ModelProfileView, status_code=201)
    async def import_model_profile(request: ImportModelProfileRequest) -> ModelProfileView:
        try:
            return await _service(plane).import_profile(request)
        except PipelineError as exc:
            raise _as_http(exc) from exc

    @router.get("/api/v1/model-profiles", response_model=list[ModelProfileView])
    async def list_model_profiles() -> list[ModelProfileView]:
        return await _service(plane).list_profiles()

    @router.get("/api/v1/model-profiles/{profile_id}", response_model=ModelProfileView)
    async def get_model_profile(profile_id: UUID) -> ModelProfileView:
        try:
            return await _service(plane).get_profile(profile_id)
        except PipelineError as exc:
            raise _as_http(exc) from exc

    @router.post("/api/v1/model-profiles/{profile_id}/activate", response_model=ModelProfileView)
    async def activate_model_profile(profile_id: UUID) -> ModelProfileView:
        try:
            return await _service(plane).activate_profile(profile_id)
        except PipelineError as exc:
            raise _as_http(exc) from exc

    return router


def _service(plane: Any) -> ModelProfileService:
    service = getattr(plane, "model_profiles", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": ErrorCode.ENGINE_UNAVAILABLE.value,
                    "stage": "model_profile",
                    "message": "control plane is not initialized",
                    "retryable": True,
                    "details": {},
                }
            },
        )
    return cast(ModelProfileService, service)


def _as_http(error: PipelineError) -> HTTPException:
    status = 422 if error.code is ErrorCode.MODEL_IMPORT_INVALID else 409
    if error.code is ErrorCode.MODEL_PROFILE_NOT_FOUND:
        status = 404
    return HTTPException(status_code=status, detail={"error": error.as_dict()})
