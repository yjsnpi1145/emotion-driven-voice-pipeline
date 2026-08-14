from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.unit.conftest import fake_fingerprint
from voice_pipeline.core.config import AppSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import EngineIdentity
from voice_pipeline.runtime import process as process_module
from voice_pipeline.runtime.process import RealWorkerProcessManager


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings.model_validate(
        {
            "schema_version": 1,
            "mode": "real",
            "engine_lifecycle": "exclusive_process",
            "server": {"host": "127.0.0.1", "port": 8765},
            "runtime_dir": str(tmp_path / "runtime"),
            "engine_lock_path": str(tmp_path / "engines.lock.yaml"),
            "checkpoint_lock_path": str(tmp_path / "checkpoints.lock.yaml"),
            "queue": {"max_concurrency": 1, "queue_timeout_seconds": 60},
            "engines": {
                "indextts": {
                    "base_url": "http://127.0.0.1:9871",
                    "python_executable": str(tmp_path / "index-python.exe"),
                    "repo_dir": str(tmp_path / "index"),
                    "request_timeout_seconds": 300,
                },
                "gpt_sovits": {
                    "base_url": "http://127.0.0.1:9880",
                    "python_executable": str(tmp_path / "gsv-python.exe"),
                    "repo_dir": str(tmp_path / "gsv"),
                    "request_timeout_seconds": 300,
                },
            },
        }
    )


def _manager(tmp_path: Path) -> RealWorkerProcessManager:
    return RealWorkerProcessManager(
        settings=_settings(tmp_path),
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
        jobs_root=tmp_path / "runtime" / "jobs",
        instance_id="test-instance",
        logs_root=tmp_path / "runtime" / "logs",
    )


class _FakeManagedProcess:
    instances: list[_FakeManagedProcess] = []
    terminate_result = True

    def __init__(self, **_: object) -> None:
        self.pid = 43120
        self.create_time = 1234.5
        self.alive = False
        self.terminate_calls: list[float] = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def terminate_tree(self, *, timeout: float) -> bool:
        self.terminate_calls.append(timeout)
        if self.terminate_result:
            self.alive = False
        return self.terminate_result


@pytest.mark.asyncio
async def test_failed_stop_keeps_worker_owned_and_reports_poisoned_failure(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    proc = _FakeManagedProcess()
    proc.start()
    proc.terminate_result = False
    manager._managed["gpt_sovits"] = proc  # type: ignore[assignment]
    manager._identity["gpt_sovits"] = EngineIdentity(
        worker="gpt_sovits",
        pid=proc.pid,
        create_time=proc.create_time,
        python_executable=tmp_path / "gsv-python.exe",
        fingerprint=fake_fingerprint("gpt_sovits"),
    )

    async def no_graceful_stop(engine: str) -> None:
        assert engine == "gpt_sovits"

    manager._graceful_stop = no_graceful_stop  # type: ignore[method-assign]

    with pytest.raises(PipelineError) as exc_info:
        await manager.stop_engine("gpt_sovits", deadline=asyncio.get_running_loop().time() + 0.1)

    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
    assert exc_info.value.poison_queue is True
    assert manager.running_engine("gpt_sovits") is True
    assert manager.engine_identity("gpt_sovits") is not None


@pytest.mark.asyncio
async def test_startup_publishes_identity_before_ready_and_cancellation_cleans_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeManagedProcess.instances.clear()
    _FakeManagedProcess.terminate_result = True
    monkeypatch.setattr(process_module, "ManagedProcess", _FakeManagedProcess)
    manager = _manager(tmp_path)
    entered_wait = asyncio.Event()
    release_wait = asyncio.Event()
    observed_pids: list[int | None] = []

    def ownership_changed() -> None:
        identity = manager.engine_identity("gpt_sovits")
        observed_pids.append(identity.pid if identity is not None else None)

    manager.set_state_change_callback(ownership_changed)

    async def wait_forever(engine: str, proc: _FakeManagedProcess) -> None:
        assert engine == "gpt_sovits"
        assert proc.is_alive()
        assert manager.engine_identity("gpt_sovits") is not None
        entered_wait.set()
        await release_wait.wait()

    manager._wait_ready = wait_forever  # type: ignore[method-assign]

    task = asyncio.create_task(manager.start_engine("gpt_sovits"))
    await asyncio.wait_for(entered_wait.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    proc = _FakeManagedProcess.instances[-1]
    assert proc.terminate_calls
    assert manager.running_engine("gpt_sovits") is False
    assert manager.engine_identity("gpt_sovits") is None
    assert observed_pids == [43120, None]


@pytest.mark.asyncio
async def test_start_engine_reconciles_expected_port_before_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeManagedProcess.instances.clear()
    monkeypatch.setattr(process_module, "ManagedProcess", _FakeManagedProcess)
    manager = _manager(tmp_path)
    observed: list[object] = []

    async def ready_immediately(engine: str, proc: _FakeManagedProcess) -> None:
        return None

    manager._wait_ready = ready_immediately  # type: ignore[method-assign]

    def reject_conflict(spec: object, *, timeout: float) -> None:
        observed.append((spec, timeout))
        raise PipelineError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "runtime",
            "port conflict",
            retryable=False,
        )

    monkeypatch.setattr(
        process_module,
        "reconcile_loopback_port",
        reject_conflict,
        raising=False,
    )

    with pytest.raises(PipelineError, match="port conflict"):
        await manager.start_engine("gpt_sovits")

    assert observed
    spec, timeout = observed[0]  # type: ignore[misc]
    assert spec.engine == "gpt_sovits"
    assert spec.host == "127.0.0.1"
    assert spec.port == 9880
    assert timeout == 5.0
    assert _FakeManagedProcess.instances == []


@pytest.mark.asyncio
async def test_startup_failure_poison_fails_when_spawned_worker_cannot_be_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakeManagedProcess.instances.clear()
    _FakeManagedProcess.terminate_result = False
    monkeypatch.setattr(process_module, "ManagedProcess", _FakeManagedProcess)
    monkeypatch.setattr(process_module, "reconcile_loopback_port", lambda *_, **__: None)
    manager = _manager(tmp_path)

    async def fail_readiness(engine: str, proc: _FakeManagedProcess) -> None:
        raise RuntimeError("readiness failed")

    manager._wait_ready = fail_readiness  # type: ignore[method-assign]

    with pytest.raises(PipelineError) as exc_info:
        await manager.start_engine("gpt_sovits")

    assert exc_info.value.poison_queue is True
    assert exc_info.value.details["pid"] == 43120
    assert manager.running_engine("gpt_sovits") is True
    assert manager.engine_identity("gpt_sovits") is not None
