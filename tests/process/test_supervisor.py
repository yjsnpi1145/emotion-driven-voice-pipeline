from __future__ import annotations

import asyncio
from pathlib import Path

import psutil
import pytest

from tests.unit.conftest import fake_fingerprint
from voice_pipeline.runtime.supervisor import ProcessSupervisor


@pytest.mark.asyncio
async def test_exclusive_mode_never_keeps_both_workers_running(
    fake_processes, tmp_path: Path
) -> None:
    registry = tmp_path / "processes.json"
    supervisor = ProcessSupervisor(
        mode="exclusive_process",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
        registry_path=registry,
    )
    await supervisor.start()
    await supervisor.ensure_engine("indextts")
    assert fake_processes.running_names() == {"indextts"}
    await supervisor.ensure_engine("gpt_sovits")
    assert fake_processes.running_names() == {"gpt_sovits"}
    await supervisor.stop()
    assert fake_processes.running_names() == set()


@pytest.mark.asyncio
async def test_abort_engine_terminates_parent_and_child_and_updates_registry(
    fake_processes, tmp_path: Path
) -> None:
    registry = tmp_path / "processes.json"
    supervisor = ProcessSupervisor(
        mode="exclusive_process",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
        registry_path=registry,
    )
    await supervisor.start()
    await supervisor.ensure_engine("gpt_sovits")
    identity = fake_processes.engine_identity("gpt_sovits")
    assert identity is not None
    pid = identity.pid
    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    assert children, "fake worker must spawn a real child"

    await supervisor.abort_engine("gpt_sovits", reason="timeout")

    assert fake_processes.running_names() == set()
    assert not psutil.pid_exists(pid)
    # PID registry no longer contains the aborted worker
    registry_payload = registry.read_text(encoding="utf-8")
    assert "gpt_sovits" in registry_payload
    import json

    data = json.loads(registry_payload)
    assert data["workers"]["gpt_sovits"]["pid"] is None

    # A subsequent ensure_engine can start the worker again.
    await supervisor.ensure_engine("gpt_sovits")
    assert fake_processes.running_names() == {"gpt_sovits"}
    await supervisor.stop()


@pytest.mark.asyncio
async def test_resident_mode_keeps_both_ready(fake_processes, tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(
        mode="resident",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
    )
    await supervisor.start()
    await supervisor.ensure_engine("indextts")
    await supervisor.ensure_engine("gpt_sovits")
    assert fake_processes.running_names() == {"indextts", "gpt_sovits"}
    health = supervisor.health()
    assert health.status == "ready"
    assert health.workers.indextts.state == "ready"
    assert health.workers.gpt_sovits.state == "ready"
    await supervisor.stop()
    assert fake_processes.running_names() == set()


@pytest.mark.asyncio
async def test_engine_identity_raises_when_not_ready(fake_processes, tmp_path: Path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    supervisor = ProcessSupervisor(
        mode="exclusive_process",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
    )
    await supervisor.start()
    with pytest.raises(PipelineError) as exc_info:
        supervisor.engine_identity("indextts")
    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
    await supervisor.stop()


class _StubbornProcesses:
    """stop_engine() is a no-op; running_engine always reports the engine up."""

    def running_engine(self, engine: str) -> bool:
        return True

    def running_names(self) -> set[str]:
        return {"indextts", "gpt_sovits"}

    def engine_identity(self, engine: str):
        return None

    async def start_engine(self, engine: str) -> None:
        return None

    async def stop_engine(self, engine: str, *, deadline: float | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_abort_poisons_queue_when_process_wont_die(tmp_path: Path) -> None:
    from voice_pipeline.core.errors import ErrorCode, PipelineError

    supervisor = ProcessSupervisor(
        mode="resident",
        processes=_StubbornProcesses(),
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
        registry_path=tmp_path / "processes.json",
    )
    await supervisor.start()
    with pytest.raises(PipelineError) as exc_info:
        await supervisor.abort_engine("indextts", reason="test")
    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
    assert exc_info.value.poison_queue is True


@pytest.mark.asyncio
async def test_fingerprint_returns_pinned(fake_processes, tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(
        mode="exclusive_process",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
    )
    await supervisor.start()
    fp = supervisor.fingerprint("indextts")
    assert fp.engine == "indextts"
    assert fp.source_revision == fake_fingerprint("indextts").source_revision
    with pytest.raises(ValueError, match="unknown engine"):
        supervisor.fingerprint("bogus")
    await supervisor.stop()


@pytest.mark.asyncio
async def test_health_degraded_when_tracker_unknown(fake_processes, tmp_path: Path) -> None:
    supervisor = ProcessSupervisor(
        mode="exclusive_process",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
    )
    await supervisor.start()
    await supervisor.ensure_engine("indextts")
    lease = await supervisor.begin_inference("indextts", job_id=__import__("uuid").uuid4())
    await lease.mark_unknown()
    health = supervisor.health()
    assert health.workers.indextts.state == "unknown"
    assert health.status == "degraded"
    await supervisor.stop()


@pytest.mark.asyncio
async def test_worker_reports_starting_after_process_dies(fake_processes, tmp_path: Path) -> None:
    import psutil

    supervisor = ProcessSupervisor(
        mode="resident",
        processes=fake_processes,
        fingerprints={
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        },
    )
    await supervisor.start()
    await supervisor.ensure_engine("indextts")
    identity = fake_processes.engine_identity("indextts")
    assert identity is not None
    # Kill the real process out from under the supervisor: identity is now
    # None while the supervisor still believes the engine is starting.
    psutil.Process(identity.pid).kill()
    for _ in range(100):
        if not fake_processes.running_engine("indextts"):
            break
        await asyncio.sleep(0.02)
    health = supervisor.health()
    assert health.workers.indextts.state == "starting"
    assert health.status == "degraded"
    await supervisor.stop()
