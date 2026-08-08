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
from voice_pipeline.models.chapter import ChapterRunRecord, ChapterSegmentProgress
from voice_pipeline.storage.chapter_store import ChapterStore

_WEBUI_ROOT = Path(__file__).parents[1] / "webui"
_WEBUI_FILES = {"index.html", "app.js", "styles.css"}


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

    return router


def _chapters(plane: Any) -> ChapterStore:
    store = getattr(plane, "chapter_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="chapter store is not ready")
    return cast(ChapterStore, store)


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


def progress_rows(payload: dict[str, Any]) -> tuple[ChapterSegmentProgress, ...]:
    """Small parser used by unit tests to enforce the public progress shape."""
    raw_rows = cast(list[dict[str, object]], payload["segments"])
    return tuple(ChapterSegmentProgress.model_validate(row) for row in raw_rows)
