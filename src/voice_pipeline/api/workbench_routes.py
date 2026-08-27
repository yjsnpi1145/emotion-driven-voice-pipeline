from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from voice_pipeline.api.chapter_routes import _public_run
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.regeneration_service import SegmentRegenerationService
from voice_pipeline.models.chapter import ChapterRunRecord, ChapterSegmentProgress
from voice_pipeline.models.persistence import (
    ArtifactVersionView,
    RestoreVersionInputsRequest,
    SegmentBothRegenerationRequest,
    SegmentGsvJobRequest,
    SegmentInputsPatch,
    SegmentReferenceRegenerationRequest,
)
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.segment_store import SegmentStore
from voice_pipeline.storage.version_store import VersionStore

_WEBUI_ROOT = Path(__file__).parents[1] / "webui"
_WEBUI_FILES = {
    "index.html",
    "app.js",
    "director-dnd.js",
    "director-llm-activity.js",
    "director-working-text.js",
    "director.js",
    "selection-state.js",
    "service-shutdown.js",
    "stage-progress.js",
    "styles.css",
}


def build_workbench_router(plane: Any) -> APIRouter:
    """Serve only packaged, local workbench assets and public status data."""
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_WEBUI_ROOT / "index.html", media_type="text/html")

    @router.get("/ui/{asset_path:path}", include_in_schema=False)
    async def asset(asset_path: str) -> FileResponse:
        if asset_path not in _WEBUI_FILES:
            raise HTTPException(status_code=404, detail="static asset not found")
        return FileResponse(_WEBUI_ROOT / asset_path)

    @router.get("/api/v1/chapters")
    async def list_chapters() -> list[dict[str, Any]]:
        return [_public_run(item) for item in await _chapters(plane).list_runs(limit=100)]

    @router.get("/api/v1/llm/activity")
    async def llm_activity() -> dict[str, Any]:
        director = getattr(plane, "llm_client", None)
        activity = getattr(director, "activity", None)
        if activity is None:
            raise HTTPException(status_code=503, detail="LLM activity is not ready")
        snapshot = await activity.snapshot()
        return cast(dict[str, Any], snapshot.model_dump(mode="json"))

    @router.get("/api/v1/chapters/{run_id}/progress")
    async def chapter_progress(run_id: UUID) -> dict[str, Any]:
        return await _progress_payload(plane, run_id)

    @router.get("/api/v1/chapters/{run_id}/events")
    async def chapter_events(run_id: UUID) -> StreamingResponse:
        await _run(plane, run_id)
        return StreamingResponse(
            _event_stream(plane, run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/v1/segments/{segment_id}/regenerate-reference", status_code=202)
    async def regenerate_reference(
        segment_id: UUID, request: SegmentReferenceRegenerationRequest
    ) -> dict[str, str]:
        try:
            context = await _regeneration(plane).submit_reference(segment_id, request)
        except (KeyError, PipelineError) as exc:
            raise _regeneration_error(exc) from exc
        return _submitted(context)

    @router.post("/api/v1/segments/{segment_id}/regenerate-gsv", status_code=202)
    async def regenerate_gsv(segment_id: UUID, request: SegmentGsvJobRequest) -> dict[str, str]:
        try:
            context = await _regeneration(plane).submit_gsv(segment_id, request)
        except (KeyError, PipelineError) as exc:
            raise _regeneration_error(exc) from exc
        return _submitted(context)

    @router.post("/api/v1/segments/{segment_id}/regenerate-both", status_code=202)
    async def regenerate_both(
        segment_id: UUID, request: SegmentBothRegenerationRequest
    ) -> dict[str, str]:
        try:
            context = await _regeneration(plane).submit_both(
                segment_id,
                request_id=request.request_id,
                base_voice_path=request.base_voice_path,
                model_profile_id=request.model_profile_id,
            )
        except (KeyError, PipelineError) as exc:
            raise _regeneration_error(exc) from exc
        return _submitted(context)

    @router.get("/api/v1/segments/{segment_id}/history")
    async def segment_history(segment_id: UUID) -> dict[str, Any]:
        try:
            segment = await _segments(plane).get_segment(segment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment not found") from exc
        versions = await _versions(plane).list_versions(segment_id)
        return {
            "segment_id": str(segment_id),
            "selection_revision": segment.selection_revision,
            "state": await _segment_state(segment, _versions(plane)),
            "reference": [
                _public_version(item, active_id=segment.active_ref_version_id)
                for item in versions
                if item.artifact_type == "reference"
            ],
            "gsv": [
                _public_version(item, active_id=segment.active_gsv_version_id)
                for item in versions
                if item.artifact_type == "gsv"
            ],
        }

    @router.post("/api/v1/segments/{segment_id}/versions/{version_id}/restore-inputs")
    async def restore_version_inputs(
        segment_id: UUID, version_id: UUID, request: RestoreVersionInputsRequest
    ) -> dict[str, Any]:
        try:
            version = await _versions(plane).get_version(version_id)
            if version.segment_id != segment_id:
                raise PipelineError(
                    ErrorCode.VERSION_CONFLICT,
                    "versions",
                    "version belongs to a different segment",
                    retryable=False,
                )
            patch = _restore_patch(version, request)
            record = await _segments(plane).patch_inputs(segment_id, patch)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="segment or version not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return record.model_dump(mode="json")

    @router.post("/api/v1/chapters/{run_id}/compose")
    async def recompose_chapter(run_id: UUID) -> dict[str, Any]:
        service = getattr(plane, "chapter_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="chapter service is not ready")
        try:
            run = await service.recompose(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="chapter run not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return _public_run(cast(ChapterRunRecord, run))

    return router


def _chapters(plane: Any) -> ChapterStore:
    store = getattr(plane, "chapter_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="chapter store is not ready")
    return cast(ChapterStore, store)


def _regeneration(plane: Any) -> SegmentRegenerationService:
    service = getattr(plane, "regeneration", None)
    if service is None:
        raise HTTPException(status_code=503, detail="regeneration service is not ready")
    return cast(SegmentRegenerationService, service)


def _segments(plane: Any) -> SegmentStore:
    store = getattr(plane, "segment_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="segment store is not ready")
    return cast(SegmentStore, store)


def _versions(plane: Any) -> VersionStore:
    store = getattr(plane, "version_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="version store is not ready")
    return cast(VersionStore, store)


async def _run(plane: Any, run_id: UUID) -> ChapterRunRecord:
    try:
        return await _chapters(plane).get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="chapter run not found") from exc


async def _progress_payload(plane: Any, run_id: UUID) -> dict[str, Any]:
    run = await _run(plane, run_id)
    progress = await _chapters(plane).progress(run_id)
    return {
        "run_id": str(run.run_id),
        "task_id": str(run.task_id),
        "status": run.status,
        "segments": [item.model_dump(mode="json") for item in progress],
    }


async def _event_stream(plane: Any, run_id: UUID) -> AsyncIterator[str]:
    previous: str | None = None
    heartbeat_ticks = 0
    try:
        while True:
            payload = await _progress_payload(plane, run_id)
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if serialized != previous:
                yield _sse("chapter_progress", payload)
                previous = serialized
            heartbeat_ticks += 1
            if heartbeat_ticks >= 30:
                yield _sse("heartbeat", {"run_id": str(run_id)})
                heartbeat_ticks = 0
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return


def _sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _submitted(context: Any) -> dict[str, str]:
    return {
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "status": "queued",
        "status_url": f"/api/v1/jobs/{context.job_id}",
    }


def _regeneration_error(exc: KeyError | PipelineError) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="segment not found")
    return HTTPException(status_code=422, detail={"error": exc.as_dict()})


async def _segment_state(segment: Any, versions: VersionStore) -> dict[str, str]:
    reference = await _active_version(versions, segment.active_ref_version_id)
    gsv = await _active_version(versions, segment.active_gsv_version_id)
    reference_status = "missing"
    if reference is not None:
        matches = (
            reference.input_snapshot.get("ref_text_cn") == segment.ref_text_cn
            and reference.input_snapshot.get("emotion_vector")
            == list(segment.current_emotion_vector)
            and reference.input_snapshot.get("seed") == segment.seed
        )
        reference_status = "ready" if matches else "draft_pending"
    gsv_status = "missing"
    if gsv is not None:
        matches = (
            gsv.ref_version_id == segment.active_ref_version_id
            and gsv.input_snapshot.get("text") == segment.synthesis_text
            and gsv.input_snapshot.get("speed_factor") == segment.speed_factor
            and gsv.input_snapshot.get("seed") == segment.seed
        )
        gsv_status = "ready" if matches else "stale"
    return {"reference": reference_status, "gsv": gsv_status}


async def _active_version(
    versions: VersionStore, version_id: UUID | None
) -> ArtifactVersionView | None:
    if version_id is None:
        return None
    try:
        return await versions.get_version(version_id)
    except KeyError:
        return None


def _public_version(version: ArtifactVersionView, *, active_id: UUID | None) -> dict[str, Any]:
    return {
        "version_id": str(version.version_id),
        "artifact_type": version.artifact_type,
        "source_job_id": str(version.source_job_id),
        "ref_version_id": str(version.ref_version_id) if version.ref_version_id else None,
        "blob_sha256": version.blob_sha256,
        "input_snapshot": _without_paths(version.input_snapshot),
        "quality_result": version.quality_result,
        "state": version.state,
        "created_at": version.created_at_utc.isoformat() if version.created_at_utc else None,
        "active": version.version_id == active_id,
        "audio_url": f"/api/v1/versions/{version.version_id}/audio",
    }


def _without_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_paths(item)
            for key, item in value.items()
            if "path" not in str(key).casefold()
        }
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    return value


def _restore_patch(
    version: ArtifactVersionView, request: RestoreVersionInputsRequest
) -> SegmentInputsPatch:
    snapshot = version.input_snapshot
    values: dict[str, Any] = {
        "expected_ref_draft_revision": request.expected_ref_draft_revision,
        "expected_gsv_draft_revision": request.expected_gsv_draft_revision,
    }
    if version.artifact_type == "reference":
        values.update(
            ref_text_cn=snapshot["ref_text_cn"],
            current_emotion_vector=snapshot["emotion_vector"],
            seed=snapshot["seed"],
        )
    else:
        values.update(
            synthesis_text=snapshot["text"],
            speed_factor=snapshot["speed_factor"],
            seed=snapshot["seed"],
        )
    try:
        return SegmentInputsPatch.model_validate(values)
    except (KeyError, ValueError) as exc:
        raise PipelineError(
            ErrorCode.DATABASE_INTEGRITY_FAILED,
            "versions",
            "version snapshot cannot restore segment inputs",
            retryable=False,
        ) from exc


def progress_rows(payload: dict[str, Any]) -> tuple[ChapterSegmentProgress, ...]:
    """Small parser used by unit tests to enforce the public progress shape."""
    raw_rows = cast(list[dict[str, object]], payload["segments"])
    return tuple(ChapterSegmentProgress.model_validate(row) for row in raw_rows)
