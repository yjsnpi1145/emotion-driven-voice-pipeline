from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voice_pipeline.core.config import AppSettings
from voice_pipeline.runtime.fingerprints import sha256_file


@dataclass
class DoctorResult:
    status: str
    codes: list[str] = field(default_factory=list)


def _normalize_windows(path: str) -> str:
    return str(Path(path)).replace("\\", "/").lower()


def validate_doctor_payload(payload: dict[str, Any]) -> DoctorResult:
    """Validate a doctor payload; returns ready/failed plus failure codes."""
    codes: list[str] = []

    control = payload.get("control") or {}
    workers = payload.get("workers") or {}
    index_worker = workers.get("indextts") or {}
    gsv_worker = workers.get("gpt_sovits") or {}

    # 1. three interpreters must be pairwise distinct and report 3.11
    pythons = {
        "control": _normalize_windows(str(control.get("python_executable", ""))),
        "indextts": _normalize_windows(str(index_worker.get("python_executable", ""))),
        "gpt_sovits": _normalize_windows(str(gsv_worker.get("python_executable", ""))),
    }
    if len(set(pythons.values())) != 3:
        codes.append("ENVIRONMENTS_NOT_ISOLATED")
    for key in ("control", "indextts", "gpt_sovits"):
        version = ""
        if key == "control":
            version = str(control.get("python_version", ""))
        else:
            version = str(workers[key].get("python_version", ""))
        if not version.startswith("3.11"):
            codes.append("PYTHON_VERSION_NOT_311")

    # 2. lifecycle-aware worker states
    lifecycle = payload.get("engine_lifecycle")
    states = [
        index_worker.get("state"),
        gsv_worker.get("state"),
    ]
    if lifecycle == "resident":
        if not all(state == "ready" for state in states):
            codes.append("RESIDENT_WORKER_NOT_READY")
    elif lifecycle == "exclusive_process":
        ready = sum(1 for state in states if state == "ready")
        stopped = sum(1 for state in states if state == "stopped_expected")
        if not (ready <= 1 and ready + stopped == 2):
            codes.append("EXCLUSIVE_WORKER_STATE_INVALID")
    else:
        codes.append("LIFECYCLE_UNKNOWN")

    # 3. checkpoint / env / uv lock digests must match
    for engine in ("indextts", "gpt_sovits"):
        digests = (workers.get(engine) or {}).get("digest_mismatch")
        if digests:
            codes.append("CHECKPOINT_DIGEST_MISMATCH")

    if payload.get("uv_lock_mismatch"):
        codes.append("UV_LOCK_MISMATCH")
    if payload.get("env_lock_mismatch"):
        codes.append("ENV_LOCK_MISMATCH")
    if payload.get("inventory_mismatch"):
        codes.append("INVENTORY_MISMATCH")

    # 4. PID registry must not contain stale PIDs
    if payload.get("pid_registry_stale"):
        codes.append("PID_REGISTRY_STALE")

    # 5. queue max concurrency must be exactly 1
    queue = payload.get("gpu_queue") or {}
    if queue.get("max_concurrency") != 1:
        codes.append("QUEUE_CONCURRENCY_INVALID")

    # 6. model revisions must match locks
    if payload.get("model_revision_mismatch"):
        codes.append("MODEL_REVISION_MISMATCH")

    # 7. real mode requires CUDA
    if payload.get("mode") == "real":
        cuda = payload.get("cuda") or {}
        if not cuda.get("available"):
            codes.append("CUDA_UNAVAILABLE")

    return DoctorResult(status="ready" if not codes else "failed", codes=codes)


def build_doctor_payload(
    settings: AppSettings,
    *,
    runtime_health: Any,
    queue_stats: Any,
    control_instance_id: str,
    audit_log: Path,
    engine_lock_path: Path,
    checkpoint_lock_path: Path,
    environment_digests: dict[str, dict[str, str]] | None = None,
    uv_lock_digest: dict[str, str] | None = None,
    live_inventory_matches: dict[str, bool] | None = None,
    pid_registry_stale: bool = False,
    model_revisions_match: bool = True,
    cuda: dict[str, Any] | None = None,
    source_revisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    workers: dict[str, Any] = {}
    for engine in ("indextts", "gpt_sovits"):
        worker = getattr(runtime_health.workers, engine)
        workers[engine] = {
            "state": worker.state,
            "pid": worker.pid,
            "create_time": worker.create_time,
            "python_executable": str(worker.python_executable),
            "python_version": worker.python_version,
            "source_revision": worker.source_revision,
            "active_inference": worker.active_inference,
            "digest_mismatch": False,
        }
    env_digests = environment_digests or {}
    workers["indextts"]["digest_mismatch"] = _digest_mismatch(env_digests.get("indextts"))
    workers["gpt_sovits"]["digest_mismatch"] = _digest_mismatch(env_digests.get("gpt_sovits"))
    return {
        "schema_version": 1,
        "mode": settings.mode,
        "engine_lifecycle": settings.engine_lifecycle,
        "control": {
            "pid": os.getpid(),
            "instance_id": control_instance_id,
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "audit_log": str(audit_log),
        },
        "workers": workers,
        "gpu_queue": {
            "state": queue_stats.state,
            "active_count": queue_stats.active_count,
            "queued_count": queue_stats.queued_count,
            "max_active_observed": queue_stats.max_active_observed,
            "max_concurrency": queue_stats.max_concurrency,
        },
        "engine_lock_sha256": sha256_file(engine_lock_path),
        "checkpoint_lock_sha256": sha256_file(checkpoint_lock_path),
        "uv_lock_mismatch": _mismatch(uv_lock_digest),
        "env_lock_mismatch": any(_digest_mismatch(entry) for entry in env_digests.values()),
        "inventory_mismatch": any(
            not matched for matched in (live_inventory_matches or {}).values()
        ),
        "pid_registry_stale": pid_registry_stale,
        "model_revision_mismatch": not model_revisions_match,
        "cuda": cuda or {"available": False, "name": None, "uuid": None},
        "source_revisions": source_revisions or {},
    }


def _mismatch(entry: dict[str, str] | None) -> bool:
    if entry is None:
        return False
    return entry.get("expected") != entry.get("actual")


def _digest_mismatch(entry: dict[str, str] | None) -> bool:
    return _mismatch(entry)
