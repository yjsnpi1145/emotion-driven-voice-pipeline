from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.runtime.port_recovery import PortRecoverySpec, reconcile_loopback_port


class _Owner:
    def __init__(
        self,
        *,
        pid: int,
        create_time: float,
        executable: Path,
        command: tuple[str, ...],
        parent_alive: bool,
    ) -> None:
        self.pid = pid
        self._create_time = create_time
        self._executable = executable
        self._command = command
        self._parent_alive = parent_alive

    def create_time(self) -> float:
        return self._create_time

    def exe(self) -> str:
        return str(self._executable)

    def cmdline(self) -> list[str]:
        return list(self._command)

    def parent(self) -> object | None:
        return object() if self._parent_alive else None


def _spec(tmp_path: Path) -> PortRecoverySpec:
    python = tmp_path / "gsv" / "python.exe"
    entry = tmp_path / "repo" / "api_v2.py"
    command = (
        str(python),
        str(entry),
        "-a",
        "127.0.0.1",
        "-p",
        "9880",
        "-c",
        str(tmp_path / "repo" / "GPT_SoVITS" / "configs" / "tts_infer.yaml"),
    )
    return PortRecoverySpec(
        engine="gpt_sovits",
        host="127.0.0.1",
        port=9880,
        python_executable=python,
        expected_command=command,
    )


def _listener(pid: int) -> SimpleNamespace:
    return SimpleNamespace(
        laddr=("127.0.0.1", 9880),
        status=psutil.CONN_LISTEN,
        pid=pid,
    )


def test_exact_match_parentless_worker_is_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voice_pipeline.runtime import port_recovery

    spec = _spec(tmp_path)
    owner = _Owner(
        pid=41200,
        create_time=100.25,
        executable=spec.python_executable,
        command=spec.expected_command,
        parent_alive=False,
    )
    terminated: list[tuple[int, float]] = []
    monkeypatch.setattr(port_recovery.psutil, "Process", lambda pid: owner)
    monkeypatch.setattr(
        port_recovery,
        "terminate_process_tree",
        lambda pid, create_time, timeout: terminated.append((pid, create_time)) or True,
    )
    listener_snapshots = iter(({owner.pid}, set()))
    monkeypatch.setattr(
        port_recovery,
        "_listener_pids",
        lambda host, port: next(listener_snapshots),
    )

    reconcile_loopback_port(spec, timeout=0.1)

    assert terminated == [(owner.pid, owner.create_time())]


def test_foreign_listener_is_reported_and_never_terminated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voice_pipeline.runtime import port_recovery

    spec = _spec(tmp_path)
    owner = _Owner(
        pid=41300,
        create_time=101.5,
        executable=tmp_path / "other-python.exe",
        command=(str(tmp_path / "other-python.exe"), str(tmp_path / "foreign.py")),
        parent_alive=False,
    )
    terminated: list[int] = []
    monkeypatch.setattr(port_recovery.psutil, "net_connections", lambda **_: [_listener(owner.pid)])
    monkeypatch.setattr(port_recovery.psutil, "Process", lambda pid: owner)
    monkeypatch.setattr(
        port_recovery,
        "terminate_process_tree",
        lambda pid, create_time, timeout: terminated.append(pid) or True,
    )

    with pytest.raises(PipelineError) as exc_info:
        reconcile_loopback_port(spec, timeout=0.1)

    assert exc_info.value.code == ErrorCode.ENGINE_UNAVAILABLE
    assert exc_info.value.details["port"] == 9880
    assert exc_info.value.details["owner_pid"] == owner.pid
    assert terminated == []


def test_matching_worker_with_live_parent_is_not_treated_as_orphan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voice_pipeline.runtime import port_recovery

    spec = _spec(tmp_path)
    owner = _Owner(
        pid=41400,
        create_time=102.5,
        executable=spec.python_executable,
        command=spec.expected_command,
        parent_alive=True,
    )
    monkeypatch.setattr(port_recovery.psutil, "net_connections", lambda **_: [_listener(owner.pid)])
    monkeypatch.setattr(port_recovery.psutil, "Process", lambda pid: owner)
    monkeypatch.setattr(
        port_recovery,
        "terminate_process_tree",
        lambda pid, create_time, timeout: pytest.fail("live-parent worker must not be killed"),
    )

    with pytest.raises(PipelineError) as exc_info:
        reconcile_loopback_port(spec, timeout=0.1)

    assert exc_info.value.details["reason"] == "owner_not_orphaned"


def test_matching_orphan_that_survives_termination_poison_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voice_pipeline.runtime import port_recovery

    spec = _spec(tmp_path)
    owner = _Owner(
        pid=41500,
        create_time=103.5,
        executable=spec.python_executable,
        command=spec.expected_command,
        parent_alive=False,
    )
    monkeypatch.setattr(port_recovery.psutil, "net_connections", lambda **_: [_listener(owner.pid)])
    monkeypatch.setattr(port_recovery.psutil, "Process", lambda pid: owner)
    monkeypatch.setattr(port_recovery, "terminate_process_tree", lambda *_, **__: False)

    with pytest.raises(PipelineError) as exc_info:
        reconcile_loopback_port(spec, timeout=0.1)

    assert exc_info.value.poison_queue is True
    assert exc_info.value.details["owner_pid"] == owner.pid


def test_listener_child_is_reaped_through_exact_match_worker_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from voice_pipeline.runtime import port_recovery

    spec = _spec(tmp_path)
    worker_root = _Owner(
        pid=41600,
        create_time=104.5,
        executable=spec.python_executable,
        command=spec.expected_command,
        parent_alive=False,
    )

    class ListenerChild(_Owner):
        def parent(self) -> object:
            return worker_root

    listener_child = ListenerChild(
        pid=41601,
        create_time=104.75,
        executable=tmp_path / "base-python.exe",
        command=(str(tmp_path / "base-python.exe"), *spec.expected_command[1:]),
        parent_alive=True,
    )
    terminated: list[tuple[int, float]] = []
    listener_snapshots = iter(({listener_child.pid}, set()))
    monkeypatch.setattr(
        port_recovery,
        "_listener_pids",
        lambda host, port: next(listener_snapshots),
    )
    monkeypatch.setattr(port_recovery.psutil, "Process", lambda pid: listener_child)
    monkeypatch.setattr(
        port_recovery,
        "terminate_process_tree",
        lambda pid, create_time, timeout: terminated.append((pid, create_time)) or True,
    )

    reconcile_loopback_port(spec, timeout=0.1)

    assert terminated == [(worker_root.pid, worker_root.create_time())]
