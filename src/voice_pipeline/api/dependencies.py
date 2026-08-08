from __future__ import annotations

import hashlib
import importlib
from typing import Any
from uuid import uuid4

from voice_pipeline.core.config import AppSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import EngineFingerprint, WorkerName
from voice_pipeline.runtime.fingerprints import compute_engine_fingerprint


def fingerprint_from_challenge(engine: WorkerName, challenge: str) -> EngineFingerprint:
    """Deterministic full fingerprint derived from engine + random challenge.

    Used only by ``external_test`` mode so the fake server and the adapter
    receive the identical immutable object.
    """

    def h(field: str) -> str:
        return hashlib.sha256(f"{engine}:{challenge}:{field}".encode()).hexdigest()

    return EngineFingerprint(
        schema_version=1,
        engine=engine,
        source_revision=f"external-test:{challenge[:16]}",
        model_revision="external-test",
        engine_lock_sha256=h("engine-lock"),
        checkpoint_lock_sha256=h("checkpoint-lock"),
        environment_lock_sha256=h("environment-lock"),
        runtime_config_sha256=h("runtime-config"),
    )


def _require_module(module: str, attr: str) -> Any:
    try:
        imported = importlib.import_module(module)
    except ImportError as exc:
        raise PipelineError(
            ErrorCode.CONFIG_INVALID,
            "dependencies",
            f"required dependency module unavailable: {module}",
            retryable=False,
        ) from exc
    return getattr(imported, attr)


def build_dependencies(settings: AppSettings) -> tuple[Any, Any, Any]:
    """Strict per-mode dependency injection (no test-specific branches)."""
    if settings.mode == "fake":
        from voice_pipeline.core.pipeline import NoopEngineRuntime
        from voice_pipeline.modules.gpt_sovits.fake import FakeGptSoVitsClient
        from voice_pipeline.modules.indextts.fake import FakeIndexTTSClient

        runtime: Any = NoopEngineRuntime()
        index: Any = FakeIndexTTSClient()
        gsv: Any = FakeGptSoVitsClient()
        return index, gsv, runtime

    if settings.mode == "external_test":
        return _build_external_test(settings)

    if settings.mode == "real":
        return _build_real(settings)

    raise PipelineError(
        ErrorCode.CONFIG_INVALID,
        "dependencies",
        f"unknown mode: {settings.mode}",
        retryable=False,
    )


def _build_external_test(settings: AppSettings) -> tuple[Any, Any, Any]:
    challenge_index = settings.engines.indextts.expected_fingerprint or {}
    challenge_gsv = settings.engines.gpt_sovits.expected_fingerprint or {}
    if not challenge_index.get("challenge") or not challenge_gsv.get("challenge"):
        raise PipelineError(
            ErrorCode.CONFIG_INVALID,
            "dependencies",
            "external_test mode requires non-empty expected_fingerprint.challenge for both engines",
            retryable=False,
        )
    index_fp = fingerprint_from_challenge("indextts", challenge_index["challenge"])
    gsv_fp = fingerprint_from_challenge("gpt_sovits", challenge_gsv["challenge"])

    jobs_root = settings.runtime_dir / "jobs"
    index_client_cls = _require_module(
        "voice_pipeline.modules.indextts.client", "IndexTTSHttpClient"
    )
    gsv_client_cls = _require_module(
        "voice_pipeline.modules.gpt_sovits.client", "GptSoVitsHttpClient"
    )
    runtime_cls = _require_module("voice_pipeline.runtime.external", "ExternalEngineRuntime")

    index = index_client_cls(
        base_url=settings.engines.indextts.base_url,
        timeout_seconds=settings.engines.indextts.request_timeout_seconds,
        jobs_root=jobs_root,
        expected_fingerprint=index_fp,
    )
    gsv = gsv_client_cls(
        base_url=settings.engines.gpt_sovits.base_url,
        timeout_seconds=settings.engines.gpt_sovits.request_timeout_seconds,
        expected_fingerprint=gsv_fp,
    )
    runtime = runtime_cls(
        settings=settings, fingerprints={"indextts": index_fp, "gpt_sovits": gsv_fp}
    )
    return index, gsv, runtime


def _build_real(settings: AppSettings) -> tuple[Any, Any, Any]:
    runtime_cls = _require_module("voice_pipeline.runtime.supervisor", "ProcessSupervisor")
    process_manager_cls = _require_module(
        "voice_pipeline.runtime.process", "RealWorkerProcessManager"
    )
    index_client_cls = _require_module(
        "voice_pipeline.modules.indextts.client", "IndexTTSHttpClient"
    )
    gsv_client_cls = _require_module(
        "voice_pipeline.modules.gpt_sovits.client", "GptSoVitsHttpClient"
    )
    locks_dir = settings.engine_lock_path.parent / "env-locks"
    fingerprints: dict[WorkerName, EngineFingerprint] = {
        "indextts": compute_engine_fingerprint(
            "indextts",
            engine_lock_path=settings.engine_lock_path,
            checkpoint_lock_path=settings.checkpoint_lock_path,
            env_lock_paths=[
                locks_dir / "index-pip-requirements.lock.txt",
                locks_dir / "index-pip-freeze.txt",
            ],
            runtime_config_path=settings.engines.indextts.repo_dir / "checkpoints" / "config.yaml",
        ),
        "gpt_sovits": compute_engine_fingerprint(
            "gpt_sovits",
            engine_lock_path=settings.engine_lock_path,
            checkpoint_lock_path=settings.checkpoint_lock_path,
            env_lock_paths=[
                locks_dir / "gsv-conda-explicit.txt",
                locks_dir / "gsv-pip-requirements.lock.txt",
                locks_dir / "gsv-pip-freeze.txt",
            ],
            runtime_config_path=(
                settings.engines.gpt_sovits.repo_dir / "GPT_SoVITS" / "configs" / "tts_infer.yaml"
            ),
        ),
    }
    instance_id = str(uuid4())
    processes = process_manager_cls(
        settings=settings,
        fingerprints=fingerprints,
        jobs_root=settings.runtime_dir / "jobs",
        instance_id=instance_id,
        logs_root=settings.runtime_dir / "logs",
    )
    runtime = runtime_cls(
        mode=settings.engine_lifecycle,
        processes=processes,
        fingerprints=fingerprints,
        registry_path=settings.runtime_dir / "run" / "processes.json",
        instance_id=instance_id,
        engine_lifecycle=settings.engine_lifecycle,
    )
    index = index_client_cls(
        base_url=settings.engines.indextts.base_url,
        timeout_seconds=settings.engines.indextts.request_timeout_seconds,
        jobs_root=settings.runtime_dir / "jobs",
        expected_fingerprint=fingerprints["indextts"],
    )
    gsv = gsv_client_cls(
        base_url=settings.engines.gpt_sovits.base_url,
        timeout_seconds=settings.engines.gpt_sovits.request_timeout_seconds,
        expected_fingerprint=fingerprints["gpt_sovits"],
    )
    return index, gsv, runtime
