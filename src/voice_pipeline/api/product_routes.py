from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException

from voice_pipeline.core.desktop_service import DesktopService
from voice_pipeline.core.errors import PipelineError
from voice_pipeline.models.desktop import (
    LocalPathsView,
    OpenFolderRequest,
    OpenFolderResult,
    PickFileRequest,
    PickFileResult,
)
from voice_pipeline.models.runtime_settings import (
    LlmConnectionTestResult,
    LlmSettingsUpdate,
    LlmSettingsView,
    QualityScoringSettingsUpdate,
    QualityScoringSettingsView,
)
from voice_pipeline.modules.llm.runtime import RuntimeDirector
from voice_pipeline.modules.quality.runtime import RuntimeQualityGate


def build_product_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/settings/llm", response_model=LlmSettingsView)
    async def get_llm_settings() -> LlmSettingsView:
        return _director(plane).view()

    @router.put("/api/v1/settings/llm", response_model=LlmSettingsView)
    async def update_llm_settings(request: LlmSettingsUpdate) -> LlmSettingsView:
        try:
            return await _director(plane).update(request)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/settings/llm/test", response_model=LlmConnectionTestResult)
    async def test_llm_settings(request: LlmSettingsUpdate) -> LlmConnectionTestResult:
        try:
            return await _director(plane).test_connection(request)
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc

    @router.get("/api/v1/settings/quality", response_model=QualityScoringSettingsView)
    async def get_quality_settings() -> QualityScoringSettingsView:
        return _quality(plane).view()

    @router.put("/api/v1/settings/quality", response_model=QualityScoringSettingsView)
    async def update_quality_settings(
        request: QualityScoringSettingsUpdate,
    ) -> QualityScoringSettingsView:
        try:
            return await _quality(plane).update(request)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/v1/local/paths", response_model=LocalPathsView)
    async def local_paths() -> LocalPathsView:
        return _desktop(plane).paths()

    @router.post("/api/v1/local/open-folder", response_model=OpenFolderResult)
    async def open_folder(request: OpenFolderRequest) -> OpenFolderResult:
        try:
            return await _desktop(plane).open_resource(request)
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/local/pick-file", response_model=PickFileResult)
    async def pick_file(request: PickFileRequest) -> PickFileResult:
        try:
            return await _desktop(plane).pick_file(request)
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/model-profiles/{profile_id}/open-folder", response_model=OpenFolderResult)
    async def open_model_profile_folder(profile_id: UUID) -> OpenFolderResult:
        try:
            return await _desktop(plane).open_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="model profile was not found") from exc
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router


def _director(plane: Any) -> RuntimeDirector:
    director = getattr(plane, "llm_client", None)
    if not isinstance(director, RuntimeDirector):
        raise HTTPException(status_code=503, detail="runtime LLM settings are not ready")
    return director


def _desktop(plane: Any) -> DesktopService:
    service = getattr(plane, "desktop_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="desktop integration is not ready")
    return cast(DesktopService, service)


def _quality(plane: Any) -> RuntimeQualityGate:
    quality = getattr(plane, "runtime_quality", None)
    if not isinstance(quality, RuntimeQualityGate):
        raise HTTPException(status_code=503, detail="runtime quality settings are not ready")
    return quality
