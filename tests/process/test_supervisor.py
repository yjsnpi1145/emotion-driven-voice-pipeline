from __future__ import annotations

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
