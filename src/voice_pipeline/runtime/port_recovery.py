from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import WorkerName


@dataclass(frozen=True)
class PortRecoverySpec:
    engine: WorkerName
    host: str
    port: int
    python_executable: Path
    expected_command: tuple[str, ...]


def terminate_process_tree(pid: int, create_time: float | None, timeout: float) -> bool:
    """Terminate one observed process tree without acting on a reused PID."""

    deadline = time.monotonic() + max(0.0, timeout)
    try:
        parent = psutil.Process(pid)
        if create_time is not None and not _same_create_time(parent.create_time(), create_time):
            return True
        children = parent.children(recursive=True)
        targets = children + [parent]
        for process in targets:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(
            targets,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(
            alive,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        return not _pid_matches(pid, create_time)
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, psutil.Error):
        return False


def _listener_pids(host: str, port: int) -> set[int | None]:
    owners: set[int | None] = set()
    for connection in psutil.net_connections(kind="tcp"):
        local = connection.laddr
        if not local:
            continue
        try:
            ip = str(local.ip)
            local_port = int(local.port)
        except AttributeError:
            ip = str(local[0])
            local_port = int(local[1])
        if (
            connection.status == psutil.CONN_LISTEN
            and local_port == port
            and ip in {host, "127.0.0.1", "::1"}
        ):
            owners.add(connection.pid)
    return owners


def reconcile_loopback_port(spec: PortRecoverySpec, *, timeout: float) -> None:
    """Reap an exact-match orphan or report a non-owned port conflict."""

    if spec.host not in {"127.0.0.1", "::1", "localhost"}:
        raise PipelineError(
            ErrorCode.CONFIG_INVALID,
            "runtime",
            f"{spec.engine} worker endpoint must be loopback-only",
            retryable=False,
            details={"engine": spec.engine, "host": spec.host, "port": spec.port},
        )
    try:
        owners = _listener_pids(spec.host, spec.port)
    except (psutil.AccessDenied, psutil.Error) as exc:
        raise _port_conflict(spec, owner_pid=None, reason="listener_inspection_failed") from exc
    if not owners:
        return

    for owner_pid in owners:
        if owner_pid is None:
            raise _port_conflict(spec, owner_pid=None, reason="owner_pid_unavailable")
        try:
            owner = psutil.Process(owner_pid)
            worker_root = _find_worker_root(owner, spec)
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.Error) as exc:
            raise _port_conflict(
                spec,
                owner_pid=owner_pid,
                reason="owner_inspection_failed",
            ) from exc

        if worker_root is None:
            raise _port_conflict(spec, owner_pid=owner_pid, reason="owner_command_mismatch")
        worker_pid = worker_root.pid
        observed_create_time = worker_root.create_time()
        parent = worker_root.parent()
        if parent is not None and _parent_is_alive(parent):
            raise _port_conflict(spec, owner_pid=worker_pid, reason="owner_not_orphaned")
        if not terminate_process_tree(worker_pid, observed_create_time, timeout):
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{spec.engine} orphan worker could not be stopped",
                retryable=False,
                details={
                    "engine": spec.engine,
                    "host": spec.host,
                    "port": spec.port,
                    "owner_pid": worker_pid,
                    "reason": "orphan_termination_failed",
                },
                poison_queue=True,
            )

    try:
        remaining = _listener_pids(spec.host, spec.port)
    except (psutil.AccessDenied, psutil.Error) as exc:
        raise _port_conflict(spec, owner_pid=None, reason="listener_recheck_failed") from exc
    if remaining:
        raise _port_conflict(
            spec,
            owner_pid=next(iter(remaining)),
            reason="port_still_occupied",
        )


def _parent_is_alive(parent: object) -> bool:
    is_running = getattr(parent, "is_running", None)
    if not callable(is_running):
        return True
    try:
        return bool(is_running())
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.Error):
        return True


def _find_worker_root(owner: psutil.Process, spec: PortRecoverySpec) -> psutil.Process | None:
    """Find an exact launch-command match in a short listener ancestry chain."""

    current: psutil.Process | None = owner
    seen: set[int] = set()
    for _ in range(8):
        if current is None or current.pid in seen:
            return None
        seen.add(current.pid)
        if _same_path(current.exe(), spec.python_executable) and _same_command(
            tuple(current.cmdline()), spec.expected_command
        ):
            return current
        current = current.parent()
    return None


def _same_command(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    if len(actual) != len(expected):
        return False
    return all(
        _same_command_token(left, right)
        for left, right in zip(actual, expected, strict=True)
    )


def _same_command_token(actual: str, expected: str) -> bool:
    expected_path = Path(expected)
    if expected_path.is_absolute():
        return _same_path(actual, expected_path)
    return actual == expected


def _same_path(actual: str | Path, expected: str | Path) -> bool:
    return str(Path(actual).resolve(strict=False)).casefold() == str(
        Path(expected).resolve(strict=False)
    ).casefold()


def _same_create_time(actual: float, expected: float) -> bool:
    return abs(actual - expected) < 0.01


def _pid_matches(pid: int, create_time: float | None) -> bool:
    try:
        process = psutil.Process(pid)
        return create_time is None or _same_create_time(process.create_time(), create_time)
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, psutil.Error):
        return True


def _port_conflict(
    spec: PortRecoverySpec,
    *,
    owner_pid: int | None,
    reason: str,
) -> PipelineError:
    return PipelineError(
        ErrorCode.ENGINE_UNAVAILABLE,
        "runtime",
        f"{spec.engine} cannot start because {spec.host}:{spec.port} is already in use",
        retryable=False,
        details={
            "engine": spec.engine,
            "host": spec.host,
            "port": spec.port,
            "owner_pid": owner_pid,
            "reason": reason,
        },
    )
