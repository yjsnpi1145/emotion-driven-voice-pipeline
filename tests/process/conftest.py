from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests.unit.conftest import fake_fingerprint
from voice_pipeline.models.schemas import EngineIdentity

_CHILD_SCRIPT = "import time; time.sleep(300)"
_PARENT_SCRIPT = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
    "time.sleep(300)"
)


class FakeChildProcesses:
    """Spawns REAL parent+child python processes to exercise tree cleanup."""

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._create_times: dict[str, float] = {}
        self._cwd = Path(__file__).resolve().parents[3]

    def running_engine(self, engine: str) -> bool:
        proc = self._procs.get(engine)
        return proc is not None and proc.poll() is None

    def running_names(self) -> set[str]:
        return {name for name in self._procs if self.running_engine(name)}

    def engine_identity(self, engine: str) -> EngineIdentity | None:
        proc = self._procs.get(engine)
        if proc is None or proc.poll() is not None:
            return None
        return EngineIdentity(
            worker=engine,  # type: ignore[arg-type]
            pid=proc.pid,
            create_time=self._create_times.get(engine, 0.0),
            python_executable=Path(sys.executable),
            fingerprint=fake_fingerprint(engine),
        )

    async def start_engine(self, engine: str) -> None:
        proc = await asyncio.to_thread(
            subprocess.Popen,
            [sys.executable, "-c", _PARENT_SCRIPT],
            cwd=str(self._cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self._procs[engine] = proc
        self._create_times[engine] = await asyncio.to_thread(
            lambda: psutil.Process(proc.pid).create_time()
        )

    async def stop_engine(self, engine: str, *, deadline: float | None = None) -> None:
        proc = self._procs.pop(engine, None)
        self._create_times.pop(engine, None)
        if proc is None:
            return
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def fake_processes():
    return FakeChildProcesses()
