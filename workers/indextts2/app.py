from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from voice_pipeline.models.schemas import EngineFingerprint, WorkerHealth

from .schemas import WorkerSynthesisRequest


def _validate_output(output_path: Path, jobs_root: Path) -> None:
    if not output_path.is_absolute():
        raise HTTPException(status_code=400, detail="output_path must be absolute")
    if output_path.is_symlink():
        raise HTTPException(status_code=403, detail="symlink output not allowed")
    resolved = output_path.resolve()
    if not resolved.is_relative_to(jobs_root):
        raise HTTPException(status_code=403, detail="output path outside jobs root")
    if resolved.suffix.lower() != ".wav":
        raise HTTPException(status_code=400, detail="output must end with .wav")
    if resolved.exists():
        raise HTTPException(status_code=409, detail="output already exists")


def _request_exit() -> None:
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        os._exit(0)


def create_worker_app(
    engine: Any,
    *,
    jobs_root: Path,
    expected_fingerprint: EngineFingerprint,
) -> FastAPI:
    jobs_root = jobs_root.resolve()
    app = FastAPI(title="indextts2-worker", version="0.1.0")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> dict[str, Any]:
        health = WorkerHealth(
            state="ready",
            pid=os.getpid(),
            create_time=0.0,
            python_executable=Path(sys.executable),
            python_version=sys.version.split()[0],
            source_revision=expected_fingerprint.source_revision,
            fingerprint=expected_fingerprint,
            preflight_ok=True,
            active_inference=0,
        )
        return health.model_dump(mode="json")

    @app.post("/v1/synthesize")
    async def synthesize(req: WorkerSynthesisRequest) -> dict[str, Any]:
        _validate_output(req.output_path, jobs_root)
        try:
            await asyncio.to_thread(engine.synthesize, req)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "code": "INDEX_ENGINE_ERROR",
                        "stage": "index",
                        "message": str(exc)[:2048],
                        "retryable": False,
                        "details": {},
                    }
                },
            ) from exc
        return {
            "request_id": str(req.request_id),
            "output_path": str(req.output_path.resolve()),
            "effective_emotion_vector": list(req.emotion_vector),
            "engine_fingerprint": expected_fingerprint.model_dump(mode="json"),
        }

    @app.post("/v1/control/stop")
    async def stop(request: Request) -> dict[str, str]:
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="loopback only")
        asyncio.get_running_loop().call_later(0.1, _request_exit)
        return {"status": "stopping"}

    return app
