from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from voice_pipeline.core.chapter_service import ChapterService
from voice_pipeline.core.errors import PipelineError
from voice_pipeline.models.chapter import ChapterRunRecord, ChapterSynthesisRequest
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.chapter_store import ChapterStore


def build_chapter_router(plane: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/chapters", status_code=202)
    async def submit_chapter(request: ChapterSynthesisRequest) -> dict[str, str]:
        if not plane.accepting:
            raise HTTPException(status_code=503, detail="control plane is not accepting work")
        try:
            run = await _service(plane).submit(request)
        except PipelineError as exc:
            raise HTTPException(status_code=422, detail={"error": exc.as_dict()}) from exc
        return {
            "run_id": str(run.run_id),
            "request_id": str(run.request_id),
            "status": run.status,
            "status_url": f"/api/v1/chapters/{run.run_id}",
        }

    @router.get("/api/v1/chapters/{run_id}")
    async def get_chapter(run_id: UUID) -> dict[str, Any]:
        return _public_run(await _get_run(plane, run_id))

    @router.delete("/api/v1/chapters/{run_id}")
    async def delete_chapter_history(run_id: UUID) -> dict[str, str]:
        try:
            await cast(ChapterStore, plane.chapter_store).delete_history_entry(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="chapter run not found") from exc
        except PipelineError as exc:
            raise HTTPException(status_code=409, detail={"error": exc.as_dict()}) from exc
        return {"status": "deleted", "run_id": str(run_id)}

    @router.get("/api/v1/chapters/{run_id}/audio")
    async def get_chapter_audio(run_id: UUID) -> FileResponse:
        run = await _get_run(plane, run_id)
        path = _finished_path(plane, run, "audio")
        if not path.is_file() or path.is_symlink() or run.final_audio is None:
            raise HTTPException(status_code=409, detail="chapter audio is missing or corrupt")
        if sha256_file(path) != run.final_audio.content_sha256:
            raise HTTPException(status_code=409, detail="chapter audio is missing or corrupt")
        return FileResponse(path, media_type="audio/wav")

    @router.get("/api/v1/chapters/{run_id}/timeline")
    async def get_chapter_timeline(run_id: UUID) -> JSONResponse:
        run = await _get_run(plane, run_id)
        path = _finished_path(plane, run, "timeline")
        if not path.is_file() or path.is_symlink():
            raise HTTPException(status_code=409, detail="chapter timeline is missing")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail="chapter timeline is corrupt") from exc
        return JSONResponse(payload)

    return router


def _service(plane: Any) -> ChapterService:
    service = getattr(plane, "chapter_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="chapter service is not ready")
    return cast(ChapterService, service)


async def _get_run(plane: Any, run_id: UUID) -> ChapterRunRecord:
    try:
        return await _service(plane).get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="chapter run not found") from exc


def _finished_path(plane: Any, run: ChapterRunRecord, kind: str) -> Path:
    if run.status != "succeeded" or run.final_relative_path is None:
        raise HTTPException(status_code=409, detail="chapter has not completed")
    artifact_store = cast(ArtifactStore, plane.artifact_store)
    root = artifact_store.root.resolve()
    audio_path = (root / run.final_relative_path).resolve()
    try:
        audio_path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="chapter output path is invalid") from exc
    return audio_path if kind == "audio" else audio_path.with_name("timeline.json")


def _public_run(run: ChapterRunRecord) -> dict[str, Any]:
    final_audio = None
    if run.final_audio is not None:
        final_audio = run.final_audio.model_dump(mode="json", exclude={"path"})
    raw_request_snapshot = run.snapshot.get("request", {})
    request_snapshot = (
        cast(dict[str, Any], raw_request_snapshot) if isinstance(raw_request_snapshot, dict) else {}
    )
    title = request_snapshot.get("title", "未命名章节")
    return cast(
        dict[str, Any],
        {
            "run_id": str(run.run_id),
            "request_id": str(run.request_id),
            "task_id": str(run.task_id),
            "title": str(title),
            "status": run.status,
            "final_audio": final_audio,
            "final_audio_url": (
                f"/api/v1/chapters/{run.run_id}/audio" if final_audio is not None else None
            ),
            "timeline": run.timeline.model_dump(mode="json") if run.timeline is not None else None,
            "error": run.error,
            "created_at": run.created_at_utc.isoformat(),
            "started_at": run.started_at_utc.isoformat() if run.started_at_utc else None,
            "finished_at": run.finished_at_utc.isoformat() if run.finished_at_utc else None,
        },
    )
