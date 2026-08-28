from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from voice_pipeline.core.director_analysis import ScriptAnalysisService
from voice_pipeline.core.director_generation import DirectorGenerationService
from voice_pipeline.core.director_preprocessing import PreprocessingService
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.role_preset_service import RolePresetService
from voice_pipeline.models.director import (
    BindRolePresetRequest,
    BulkDirectorUtterancePatch,
    CreateDirectorProjectRequest,
    CreateRolePresetRequest,
    DirectorPreprocessParagraphPatch,
    DirectorProjectRecord,
    DirectorRolePatch,
    DirectorUtterancePatch,
    ExpectedProjectRevision,
    MergeDirectorRolesRequest,
    MergeDirectorUtterancesRequest,
    NarrationSettingRequest,
    RestoreDirectorPreprocessParagraphRequest,
    RewriteDirectorPreprocessParagraphRequest,
    RolePresetRecord,
    SplitDirectorRoleRequest,
    SplitDirectorUtteranceRequest,
    UpdateDirectorPerformanceDirection,
    UpdateRolePresetRequest,
)
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.director_store import DirectorStore
from voice_pipeline.storage.reference_pool_store import ReferencePoolStore


def build_director_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/director-projects", status_code=201)
    async def create_project(request: CreateDirectorProjectRequest) -> dict[str, Any]:
        return _public_project(await _store(plane).create_project(request))

    @router.get("/api/v1/director-projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [_public_project(item) for item in await _store(plane).list_projects()]

    @router.get("/api/v1/director-projects/{project_id}")
    async def get_project(project_id: UUID) -> dict[str, Any]:
        try:
            return _public_project(await _store(plane).get_project(project_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc

    @router.patch("/api/v1/director-projects/{project_id}/performance-direction")
    async def update_performance_direction(
        project_id: UUID,
        request: UpdateDirectorPerformanceDirection,
    ) -> dict[str, Any]:
        try:
            project = await _store(plane).update_performance_direction(
                project_id,
                expected_revision=request.expected_revision,
                performance_direction=request.performance_direction,
                reapply=request.reapply,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return _public_project(project)

    @router.delete("/api/v1/director-projects/{project_id}")
    async def delete_project(project_id: UUID, request: ExpectedProjectRevision) -> dict[str, str]:
        try:
            await _store(plane).delete_project(
                project_id, expected_revision=request.expected_revision
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return {"status": "deleted", "project_id": str(project_id)}

    @router.post(
        "/api/v1/director-projects/{project_id}/preprocess",
        status_code=202,
    )
    async def preprocess(
        project_id: UUID,
        request: ExpectedProjectRevision,
    ) -> dict[str, str]:
        if await _should_start_command(
            plane,
            project_id,
            request.expected_revision,
            operation="preprocessing",
        ):
            _spawn(
                plane,
                _preprocessing(plane).run(
                    project_id,
                    expected_revision=request.expected_revision,
                ),
                project_id=project_id,
                operation="preprocessing",
                expected_status="preprocessing",
            )
        return {
            "project_id": str(project_id),
            "status": "accepted",
            "status_url": f"/api/v1/director-projects/{project_id}",
        }

    @router.get("/api/v1/director-projects/{project_id}/preprocess")
    async def preprocessing_page(
        project_id: UUID,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return _dump(
                await _store(plane).list_preprocess_paragraphs(
                    project_id,
                    offset=offset,
                    limit=limit,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc

    @router.patch(
        "/api/v1/director-projects/{project_id}"
        "/preprocess-paragraphs/{paragraph_id}"
    )
    async def patch_preprocessing_paragraph(
        project_id: UUID,
        paragraph_id: str,
        request: DirectorPreprocessParagraphPatch,
    ) -> dict[str, Any]:
        try:
            return _dump(
                await _store(plane).patch_preprocess_paragraph(
                    project_id,
                    paragraph_id,
                    expected_project_revision=request.expected_project_revision,
                    expected_revision=request.expected_revision,
                    preprocessed_text=request.preprocessed_text,
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="preprocessing paragraph not found",
            ) from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post(
        "/api/v1/director-projects/{project_id}"
        "/preprocess-paragraphs/{paragraph_id}/restore"
    )
    async def restore_preprocessing_paragraph(
        project_id: UUID,
        paragraph_id: str,
        request: RestoreDirectorPreprocessParagraphRequest,
    ) -> dict[str, Any]:
        try:
            return _dump(
                await _store(plane).restore_preprocess_paragraph(
                    project_id,
                    paragraph_id,
                    expected_project_revision=request.expected_project_revision,
                    expected_revision=request.expected_revision,
                    target=request.target,
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="preprocessing paragraph not found",
            ) from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post(
        "/api/v1/director-projects/{project_id}"
        "/preprocess-paragraphs/{paragraph_id}/rewrite"
    )
    async def rewrite_preprocessing_paragraph(
        project_id: UUID,
        paragraph_id: str,
        request: RewriteDirectorPreprocessParagraphRequest,
    ) -> dict[str, Any]:
        try:
            return _dump(
                await _preprocessing(plane).rewrite_paragraph(
                    project_id,
                    paragraph_id,
                    expected_project_revision=request.expected_project_revision,
                    expected_revision=request.expected_revision,
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="preprocessing paragraph not found",
            ) from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post(
        "/api/v1/director-projects/{project_id}/confirm-preprocessing",
        status_code=202,
    )
    async def confirm_preprocessing(
        project_id: UUID,
        request: ExpectedProjectRevision,
    ) -> dict[str, str]:
        async with _command_submission_lock(plane, project_id, "analysis"):
            if not _command_running(plane, project_id, "analysis"):
                try:
                    confirmed = await _store(plane).confirm_preprocessing(
                        project_id,
                        expected_revision=request.expected_revision,
                    )
                except KeyError as exc:
                    raise HTTPException(
                        status_code=404,
                        detail="director project not found",
                    ) from exc
                except PipelineError as exc:
                    raise _pipeline_error(exc) from exc
                _spawn(
                    plane,
                    _analysis(plane).analyze(
                        project_id,
                        expected_revision=confirmed.revision,
                    ),
                    project_id=project_id,
                    operation="analysis",
                    expected_status="analyzing",
                )
        return {
            "project_id": str(project_id),
            "status": "accepted",
            "status_url": f"/api/v1/director-projects/{project_id}",
        }

    @router.post("/api/v1/director-projects/{project_id}/analyze", status_code=202)
    async def analyze(project_id: UUID, request: ExpectedProjectRevision) -> dict[str, str]:
        if await _should_start_command(
            plane,
            project_id,
            request.expected_revision,
            operation="analysis",
            require_confirmed_preprocessing=True,
        ):
            _spawn(
                plane,
                _analysis(plane).analyze(
                    project_id, expected_revision=request.expected_revision
                ),
                project_id=project_id,
                operation="analysis",
                expected_status="analyzing",
            )
        return {
            "project_id": str(project_id),
            "status": "accepted",
            "status_url": f"/api/v1/director-projects/{project_id}",
        }

    @router.get("/api/v1/director-projects/{project_id}/roles")
    async def list_roles(project_id: UUID) -> list[dict[str, Any]]:
        try:
            return [_dump(item) for item in await _store(plane).list_roles(project_id)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc

    @router.get("/api/v1/director-projects/{project_id}/utterances")
    async def list_utterances(project_id: UUID) -> list[dict[str, Any]]:
        try:
            return [_dump(item) for item in await _store(plane).list_utterances(project_id)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc

    @router.patch("/api/v1/director-roles/{role_id}")
    async def patch_role(role_id: UUID, request: DirectorRolePatch) -> dict[str, Any]:
        try:
            return _dump(
                await _store(plane).patch_role(
                    role_id,
                    expected_revision=request.expected_revision,
                    canonical_name=request.canonical_name,
                    aliases=request.aliases,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director role not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-roles/merge")
    async def merge_roles(request: MergeDirectorRolesRequest) -> list[dict[str, Any]]:
        try:
            records = await _store(plane).merge_roles(
                request.project_id,
                source_role_ids=request.source_role_ids,
                target_role_id=request.target_role_id,
                expected_project_revision=request.expected_project_revision,
            )
            return [_dump(item) for item in records]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director role not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-roles/split")
    async def split_role(request: SplitDirectorRoleRequest) -> list[dict[str, Any]]:
        try:
            records = await _store(plane).split_role(
                request.project_id,
                source_role_id=request.source_role_id,
                utterance_ids=request.utterance_ids,
                canonical_name=request.canonical_name,
                expected_project_revision=request.expected_project_revision,
            )
            return [_dump(item) for item in records]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director role not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.patch("/api/v1/director-utterances/{utterance_id}")
    async def patch_utterance(
        utterance_id: UUID, request: DirectorUtterancePatch
    ) -> dict[str, Any]:
        changes = request.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        try:
            return _dump(
                await _store(plane).patch_utterance(
                    utterance_id,
                    expected_revision=request.expected_revision,
                    **changes,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director utterance not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-projects/{project_id}/assign-role")
    async def bulk_assign(
        project_id: UUID, request: BulkDirectorUtterancePatch
    ) -> list[dict[str, Any]]:
        if set(request.utterance_ids) != set(request.expected_revisions):
            raise HTTPException(status_code=422, detail="every selected utterance needs a revision")
        try:
            records = await _store(plane).bulk_assign_role(
                project_id,
                utterance_revisions=request.expected_revisions,
                role_id=request.role_id,
            )
            return [_dump(item) for item in records]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director role not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-utterances/{utterance_id}/split")
    async def split_utterance(
        utterance_id: UUID, request: SplitDirectorUtteranceRequest
    ) -> list[dict[str, Any]]:
        try:
            records = await _store(plane).split_utterance(
                utterance_id,
                expected_revision=request.expected_revision,
                split_at=request.split_at,
            )
            return [_dump(item) for item in records]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director utterance not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-utterances/merge")
    async def merge_utterances(
        request: MergeDirectorUtterancesRequest,
    ) -> list[dict[str, Any]]:
        try:
            records = await _store(plane).merge_utterances(
                request.left_utterance_id,
                request.right_utterance_id,
                expected_left_revision=request.expected_left_revision,
                expected_right_revision=request.expected_right_revision,
            )
            return [_dump(item) for item in records]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director utterance not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.patch("/api/v1/director-projects/{project_id}/narration")
    async def set_narration(project_id: UUID, request: NarrationSettingRequest) -> dict[str, Any]:
        try:
            return _public_project(
                await _store(plane).set_narration_enabled(
                    project_id,
                    expected_revision=request.expected_revision,
                    enabled=request.enabled,
                )
            )
        except (KeyError, PipelineError) as exc:
            if isinstance(exc, KeyError):
                raise HTTPException(status_code=404, detail="director project not found") from exc
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-projects/{project_id}/confirm-roles")
    async def confirm_roles(project_id: UUID, request: ExpectedProjectRevision) -> dict[str, Any]:
        try:
            return _public_project(
                await _store(plane).confirm_role_review(
                    project_id, expected_revision=request.expected_revision
                )
            )
        except (KeyError, PipelineError) as exc:
            if isinstance(exc, KeyError):
                raise HTTPException(status_code=404, detail="director project not found") from exc
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-projects/{project_id}/translate", status_code=202)
    async def translate(project_id: UUID, request: ExpectedProjectRevision) -> dict[str, str]:
        if await _should_start_command(
            plane,
            project_id,
            request.expected_revision,
            operation="translation",
        ):
            _spawn(
                plane,
                _analysis(plane).translate(
                    project_id, expected_revision=request.expected_revision
                ),
                project_id=project_id,
                operation="translation",
                expected_status="translating",
            )
        return {
            "project_id": str(project_id),
            "status": "accepted",
            "status_url": f"/api/v1/director-projects/{project_id}",
        }

    @router.post("/api/v1/director-projects/{project_id}/confirm-translation")
    async def confirm_translation(
        project_id: UUID, request: ExpectedProjectRevision
    ) -> dict[str, Any]:
        try:
            return _public_project(
                await _store(plane).confirm_translation(
                    project_id, expected_revision=request.expected_revision
                )
            )
        except (KeyError, PipelineError) as exc:
            if isinstance(exc, KeyError):
                raise HTTPException(status_code=404, detail="director project not found") from exc
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/director-roles/{role_id}/preset")
    async def bind_preset(role_id: UUID, request: BindRolePresetRequest) -> dict[str, Any]:
        try:
            return _dump(
                await _store(plane).bind_role_preset(
                    role_id,
                    expected_revision=request.expected_revision,
                    preset_id=request.preset_id,
                    dubbing_enabled=request.mapping_mode == "preset",
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director role not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.post("/api/v1/role-presets", status_code=201)
    async def create_preset(request: CreateRolePresetRequest) -> dict[str, Any]:
        try:
            return _public_preset(await _presets(plane).import_preset(request))
        except (FileNotFoundError, PipelineError) as exc:
            if isinstance(exc, FileNotFoundError):
                raise HTTPException(status_code=422, detail="base voice file not found") from exc
            raise _pipeline_error(exc) from exc

    @router.get("/api/v1/role-presets")
    async def list_presets() -> list[dict[str, Any]]:
        return [_public_preset(item) for item in await _presets(plane).list()]

    @router.patch("/api/v1/role-presets/{preset_id}")
    async def update_preset(preset_id: UUID, request: UpdateRolePresetRequest) -> dict[str, Any]:
        try:
            return _public_preset(await _presets(plane).update(preset_id, request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="role preset not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.delete("/api/v1/role-presets/{preset_id}")
    async def archive_preset(preset_id: UUID, request: ExpectedProjectRevision) -> dict[str, Any]:
        try:
            return _public_preset(
                await _presets(plane).archive(
                    preset_id, expected_revision=request.expected_revision
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="role preset not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc

    @router.get("/api/v1/role-presets/{preset_id}/audio")
    async def preset_audio(preset_id: UUID) -> FileResponse:
        try:
            preset = await _presets(plane).resolve(preset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="role preset not found") from exc
        if preset.status != "ready":
            raise HTTPException(status_code=409, detail="role preset audio is unavailable")
        path = _presets(plane).audio_path(preset)
        if sha256_file(path) != preset.base_voice_sha256:
            raise HTTPException(status_code=409, detail="role preset audio is corrupt")
        return FileResponse(path, media_type="audio/wav")

    @router.post("/api/v1/director-projects/{project_id}/start-generation", status_code=202)
    async def start_generation(
        project_id: UUID, request: ExpectedProjectRevision
    ) -> dict[str, str]:
        try:
            generation = await _generation(plane).start(
                project_id, expected_revision=request.expected_revision
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return {
            "project_id": str(project_id),
            "generation_id": str(generation.generation_id),
            "status": generation.status,
            "status_url": f"/api/v1/director-projects/{project_id}/progress",
        }

    @router.get("/api/v1/director-projects/{project_id}/progress")
    async def generation_progress(project_id: UUID) -> dict[str, Any]:
        try:
            generation, items = await _generation(plane).get_for_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        public_items: list[dict[str, Any]] = []
        for item in items:
            payload = _dump(item)
            payload["reference_pool"] = None
            if item.reference_pool_entry_id is not None:
                try:
                    entry = await _reference_pool(plane).get(item.reference_pool_entry_id)
                except KeyError:
                    pass
                else:
                    payload["reference_pool"] = {
                        "entry_id": str(entry.entry_id),
                        "prompt_text": entry.prompt_text,
                        "emotion_bucket": entry.emotion_bucket,
                        "revision": entry.revision,
                        "attempt": entry.attempt,
                        "status": entry.status,
                        "seed": entry.seed,
                    }
            public_items.append(payload)
        return {
            "generation": _public_generation(generation) if generation is not None else None,
            "items": public_items,
        }

    @router.post("/api/v1/director-projects/{project_id}/resume-generation", status_code=202)
    async def resume_generation(
        project_id: UUID, request: ExpectedProjectRevision
    ) -> dict[str, Any]:
        try:
            generation = await _generation(plane).resume(
                project_id, expected_revision=request.expected_revision
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return _public_generation(generation)

    @router.post(
        "/api/v1/director-generations/{generation_id}/utterances/{utterance_id}/rebuild-pooled-reference",
        status_code=202,
    )
    async def rebuild_pooled_reference(
        generation_id: UUID, utterance_id: UUID
    ) -> dict[str, str]:
        try:
            await _generation(plane).force_rebuild_pool_reference(
                generation_id, utterance_id
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="director generation item not found"
            ) from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return {
            "generation_id": str(generation_id),
            "utterance_id": str(utterance_id),
            "status": "queued",
        }

    @router.post("/api/v1/director-projects/{project_id}/recompose", status_code=202)
    async def recompose(project_id: UUID, request: ExpectedProjectRevision) -> dict[str, Any]:
        try:
            generation = await _generation(plane).recompose(
                project_id, expected_revision=request.expected_revision
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        except PipelineError as exc:
            raise _pipeline_error(exc) from exc
        return _public_generation(generation)

    @router.get("/api/v1/director-projects/{project_id}/audio")
    async def generation_audio(project_id: UUID) -> FileResponse:
        try:
            generation, _ = await _generation(plane).get_for_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        if (
            generation is None
            or generation.status != "succeeded"
            or generation.final_relative_path is None
        ):
            raise HTTPException(status_code=409, detail="director audio is not ready")
        root = plane.artifact_store.root.resolve()
        path = (root / generation.final_relative_path).resolve()
        try:
            path.relative_to((root / "directors").resolve())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="director audio path is invalid") from exc
        if not path.is_file() or path.is_symlink():
            raise HTTPException(status_code=409, detail="director audio is unavailable")
        return FileResponse(path, media_type="audio/wav")

    @router.get("/api/v1/director-projects/{project_id}/sentence-audio.zip")
    async def generation_sentence_archive(project_id: UUID) -> FileResponse:
        try:
            generation, _ = await _generation(plane).get_for_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="director project not found") from exc
        if (
            generation is None
            or generation.status != "succeeded"
            or generation.final_relative_path is None
        ):
            raise HTTPException(status_code=409, detail="director sentence audio is not ready")
        try:
            archive_path = _resolve_sentence_archive_path(
                plane.artifact_store.root,
                generation.final_relative_path,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="director sentence audio path is invalid"
            ) from exc
        if not archive_path.is_file() or archive_path.is_symlink():
            raise HTTPException(status_code=409, detail="director sentence audio is unavailable")
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"director-{project_id}-sentences.zip",
        )

    return router


def _store(plane: Any) -> DirectorStore:
    value = getattr(plane, "director_store", None)
    if value is None:
        raise HTTPException(status_code=503, detail="director store is not ready")
    return cast(DirectorStore, value)


def _analysis(plane: Any) -> ScriptAnalysisService:
    value = getattr(plane, "director_analysis", None)
    if value is None:
        raise HTTPException(status_code=503, detail="director analysis is not ready")
    return cast(ScriptAnalysisService, value)


def _preprocessing(plane: Any) -> PreprocessingService:
    value = getattr(plane, "director_preprocessing", None)
    if value is None:
        raise HTTPException(status_code=503, detail="director preprocessing is not ready")
    return cast(PreprocessingService, value)


def _presets(plane: Any) -> RolePresetService:
    value = getattr(plane, "role_presets", None)
    if value is None:
        raise HTTPException(status_code=503, detail="role presets are not ready")
    return cast(RolePresetService, value)


def _generation(plane: Any) -> DirectorGenerationService:
    value = getattr(plane, "director_generation", None)
    if value is None:
        raise HTTPException(status_code=503, detail="director generation is not ready")
    return cast(DirectorGenerationService, value)


def _reference_pool(plane: Any) -> ReferencePoolStore:
    value = getattr(plane, "reference_pool", None)
    if value is None:
        raise HTTPException(status_code=503, detail="reference pool is not ready")
    return cast(ReferencePoolStore, value)


async def _require_project_revision(plane: Any, project_id: UUID, revision: int) -> None:
    try:
        project = await _store(plane).get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="director project not found") from exc
    if project.revision != revision:
        raise _pipeline_error(
            PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "director",
                "director data changed; refresh before submitting",
                retryable=False,
            )
        )


def _command_running(plane: Any, project_id: UUID, operation: str) -> bool:
    command = plane.director_commands.get((project_id, operation))
    return command is not None and not command.done()


def _command_submission_lock(
    plane: Any,
    project_id: UUID,
    operation: str,
) -> asyncio.Lock:
    locks = getattr(plane, "director_submission_locks", None)
    if locks is None:
        locks = {}
        plane.director_submission_locks = locks
    return cast(dict[tuple[UUID, str], asyncio.Lock], locks).setdefault(
        (project_id, operation),
        asyncio.Lock(),
    )


async def _should_start_command(
    plane: Any,
    project_id: UUID,
    revision: int,
    *,
    operation: str,
    require_confirmed_preprocessing: bool = False,
) -> bool:
    if _command_running(plane, project_id, operation):
        return False
    try:
        await _require_project_revision(plane, project_id, revision)
        if require_confirmed_preprocessing:
            project = await _store(plane).get_project(project_id)
            if project.preprocessed_text is None or project.status not in {
                "analyzing",
                "role_review",
            }:
                raise _pipeline_error(
                    PipelineError(
                        ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                        "director",
                        "confirm the preprocessing draft before analysis",
                        retryable=False,
                    )
                )
    except HTTPException:
        if _command_running(plane, project_id, operation):
            return False
        raise
    return True


def _spawn(
    plane: Any,
    coroutine: Coroutine[Any, Any, object],
    *,
    project_id: UUID,
    operation: str,
    expected_status: str,
) -> bool:
    key = (project_id, operation)
    commands: dict[tuple[UUID, str], asyncio.Task[object]] = plane.director_commands
    existing = commands.get(key)
    if existing is not None and not existing.done():
        coroutine.close()
        return False

    async def run() -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await _store(plane).record_command_failure(
                project_id,
                expected_status=expected_status,
                operation=operation,
                error=_background_error(exc),
            )

    task = asyncio.create_task(run())
    tasks: set[asyncio.Task[object]] = plane.director_tasks
    tasks.add(task)
    commands[key] = task

    def done(completed: asyncio.Task[object]) -> None:
        tasks.discard(completed)
        if commands.get(key) is completed:
            commands.pop(key, None)
        try:
            completed.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(done)
    return True


def _background_error(error: BaseException) -> dict[str, object]:
    if isinstance(error, PipelineError):
        return cast(dict[str, object], error.as_dict())
    return {
        "code": "INTERNAL_ERROR",
        "stage": "director",
        "message": "director background command failed",
        "retryable": True,
        "details": {},
    }


def _public_preset(record: RolePresetRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json", exclude={"base_voice_relative_path"})
    payload["audio_url"] = f"/api/v1/role-presets/{record.preset_id}/audio"
    return payload


def _public_project(record: DirectorProjectRecord) -> dict[str, Any]:
    payload = record.model_dump(
        mode="json",
        exclude={
            "final_relative_path",
            "timeline",
            "structural_text",
            "preprocessed_text",
        },
    )
    payload["audio_url"] = (
        f"/api/v1/director-projects/{record.project_id}/audio"
        if record.status == "succeeded"
        else None
    )
    payload["sentence_audio_url"] = (
        f"/api/v1/director-projects/{record.project_id}/sentence-audio.zip"
        if record.status == "succeeded"
        else None
    )
    return payload


def _public_generation(record: Any) -> dict[str, Any]:
    payload = record.model_dump(
        mode="json",
        exclude={"snapshot", "final_relative_path", "timeline"},
    )
    payload["audio_url"] = (
        f"/api/v1/director-projects/{record.project_id}/audio"
        if record.status == "succeeded"
        else None
    )
    payload["sentence_audio_url"] = (
        f"/api/v1/director-projects/{record.project_id}/sentence-audio.zip"
        if record.status == "succeeded"
        else None
    )
    return cast(dict[str, Any], payload)


def _dump(record: Any) -> dict[str, Any]:
    return cast(dict[str, Any], record.model_dump(mode="json"))


def _resolve_sentence_archive_path(root: Path, final_relative_path: str) -> Path:
    root = root.resolve()
    directors_root = (root / "directors").resolve()
    final_candidate = root / final_relative_path
    archive_candidate = final_candidate.parent / "sentences.zip"
    final_path = final_candidate.resolve()
    archive_path = archive_candidate.resolve()
    final_path.relative_to(directors_root)
    archive_path.relative_to(directors_root)
    if _path_chain_contains_symlink(final_candidate, root) or _path_chain_contains_symlink(
        archive_candidate, root
    ):
        raise ValueError("director sentence audio path contains a symlink")
    return archive_path


def _path_chain_contains_symlink(candidate: Path, root: Path) -> bool:
    current = candidate
    while True:
        if current.is_symlink():
            return True
        if current == root:
            return False
        parent = current.parent
        if parent == current:
            raise ValueError("director sentence audio path is outside the artifact root")
        current = parent


def _pipeline_error(error: PipelineError) -> HTTPException:
    status = (
        409
        if error.code
        in {
            ErrorCode.VERSION_CONFLICT,
            ErrorCode.DIRECTOR_STATE_CONFLICT,
            ErrorCode.DIRECTOR_REVIEW_REQUIRED,
            ErrorCode.ROLE_PRESET_UNAVAILABLE,
        }
        else 422
    )
    return HTTPException(status_code=status, detail={"error": error.as_dict()})
