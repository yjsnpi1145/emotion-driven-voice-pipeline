from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from voice_pipeline.api.dependencies import build_dependencies
from voice_pipeline.api.model_profile_routes import build_model_profile_router
from voice_pipeline.api.routes import build_router
from voice_pipeline.core.config import AppSettings
from voice_pipeline.core.dispatcher import DurableJobDispatcher
from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.job_executor import JobExecutor
from voice_pipeline.core.jobs import InMemoryJobRegistry
from voice_pipeline.core.model_profile_service import ModelProfileService
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.runtime.audit import EngineAuditWriter
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.database import Database
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.model_importer import ModelProfileImporter
from voice_pipeline.storage.model_profile_store import SqliteModelProfileStore
from voice_pipeline.storage.recovery import StorageRecovery


class ControlPlane:
    """Owns the runtime, queue, registry and the idempotent shutdown coordinator."""

    def __init__(
        self,
        settings: AppSettings,
        index: Any,
        gsv: Any,
        runtime: Any,
        audit: EngineAuditWriter,
        registry: Any,
        queue: SerialGpuQueue,
        service: SynthesisService,
    ) -> None:
        self.settings = settings
        self.index = index
        self.gsv = gsv
        self.runtime = runtime
        self.audit = audit
        self.registry = registry
        self.queue = queue
        self.service = service
        self._exit_callback: Callable[[], None] | None = None
        self._accepting = True
        self._shutdown_started = False
        self.database: Database | None = None
        self.model_profiles: ModelProfileService | None = None
        self.dispatcher: DurableJobDispatcher | None = None
        self.artifact_store: ArtifactStore | None = None

    def attach_durable_state(self, database: Database, model_profiles: ModelProfileService) -> None:
        self.database = database
        self.model_profiles = model_profiles

    def set_exit_callback(self, callback: Callable[[], None]) -> None:
        self._exit_callback = callback

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def shutdown(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._accepting = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.5

        async def abort_active(dl: float) -> None:
            await self._abort_all_active(dl)

        await self.queue.stop(deadline=deadline, grace_seconds=0.5, abort_active=abort_active)
        registry = getattr(self, "registry", None)
        fail_unfinished = getattr(registry, "fail_unfinished", None)
        if fail_unfinished is not None:
            await fail_unfinished(
                error={
                    "code": "ENGINE_UNAVAILABLE",
                    "stage": "shutdown",
                    "message": "control plane shut down before job completion",
                    "retryable": False,
                    "details": {},
                }
            )
        dispatcher = getattr(self, "dispatcher", None)
        if dispatcher is not None:
            await dispatcher.stop(deadline=deadline)
        remaining = max(0.0, deadline - loop.time())
        try:
            await asyncio.wait_for(self.runtime.stop(deadline=deadline), timeout=remaining)
        except TimeoutError:
            pass
        if self._exit_callback is not None:
            self._exit_callback()

    async def _abort_all_active(self, deadline: float) -> None:
        health = self.runtime.health()
        pending = []
        for engine in ("indextts", "gpt_sovits"):
            worker = getattr(health.workers, engine)
            if worker.state in ("ready", "starting", "unknown"):
                pending.append(
                    asyncio.create_task(
                        self.runtime.abort_engine(
                            engine,
                            reason="control plane shutdown",
                            deadline=deadline,
                        )
                    )
                )
        for task in pending:
            try:
                await task
            except Exception:
                pass


def create_app(
    settings: AppSettings,
    *,
    index_client: Any = None,
    gsv_client: Any = None,
    engine_runtime: Any = None,
) -> FastAPI:
    index, gsv, runtime = build_dependencies(settings)
    if index_client is not None:
        index = index_client
    if gsv_client is not None:
        gsv = gsv_client
    if engine_runtime is not None:
        runtime = engine_runtime

    runtime_dir = settings.runtime_dir
    runtime_dir.mkdir(parents=True, exist_ok=True)
    audit = EngineAuditWriter(runtime_dir)
    registry = InMemoryJobRegistry(jobs_root=runtime_dir / "jobs")
    queue = SerialGpuQueue(queue_timeout_seconds=settings.queue.queue_timeout_seconds)
    service = SynthesisService(index=index, gsv=gsv, runtime=runtime, audit=audit)
    plane = ControlPlane(settings, index, gsv, runtime, audit, registry, queue, service)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = await Database.open(settings.storage, instance_id=uuid4(), migrate=True)
        profile_store = SqliteModelProfileStore(
            database,
            models_root=settings.model_library.models_root,
        )
        profile_importer = ModelProfileImporter(
            models_root=settings.model_library.models_root,
            allowed_import_roots=settings.model_library.allowed_import_roots,
        )
        model_profile_service = ModelProfileService(importer=profile_importer, store=profile_store)
        plane.attach_durable_state(database, model_profile_service)
        plane.service.configure_model_profile_resolver(
            model_profile_service.resolve_selected_profile,
            require_model_profile=settings.mode == "real",
        )
        plane.registry = SqliteJobStore(database, jobs_root=runtime_dir / "jobs")
        plane.artifact_store = ArtifactStore(settings.storage.artifact_root)
        plane.dispatcher = DurableJobDispatcher(
            store=plane.registry,
            queue=queue,
            executor=JobExecutor(service, jobs_root=runtime_dir / "jobs"),
            instance_id=audit.instance_id,
            queue_timeout_seconds=settings.queue.queue_timeout_seconds,
        )
        try:
            await StorageRecovery(database, plane.artifact_store).reconcile()
            await runtime.start()
            await queue.start()
            await plane.dispatcher.start()
            yield
        finally:
            await plane.shutdown()
            await database.close()

    app = FastAPI(
        title="Emotion Driven Voice Pipeline",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.plane = plane

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        from fastapi.encoders import jsonable_encoder

        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "stage": "input",
                    "message": "request validation failed",
                    "retryable": False,
                    "details": {"errors": jsonable_encoder(exc.errors())[:20]},
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            error: dict[str, Any] = detail["error"]
        else:
            error = {
                "code": "HTTP_ERROR",
                "stage": "api",
                "message": str(detail),
                "retryable": False,
                "details": {},
            }
        return JSONResponse(status_code=exc.status_code, content={"error": error})

    router = build_router(plane)
    app.include_router(router)
    app.include_router(build_model_profile_router(plane))
    return app
