from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from voice_pipeline.core.config import AppSettings
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import EngineFingerprint, EngineIdentity, WorkerName


class ManagedProcess:
    """Windows subprocess wrapper with recorded PID/create-time and tree kill."""

    def __init__(
        self,
        *,
        role: str,
        args: list[str],
        cwd: Path,
        stdout_log: Path,
        stderr_log: Path,
    ) -> None:
        self._role = role
        self._args = list(args)
        self._cwd = cwd
        self._stdout_log = stdout_log
        self._stderr_log = stderr_log
        self._process: subprocess.Popen[Any] | None = None
        self._create_time: float | None = None

    def start(self) -> None:
        self._stdout_log.parent.mkdir(parents=True, exist_ok=True)
        stdout_fh = open(self._stdout_log, "ab")
        stderr_fh = open(self._stderr_log, "ab")
        self._process = subprocess.Popen(
            self._args,
            cwd=str(self._cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout_fh,
            stderr=stderr_fh,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            self._create_time = psutil.Process(self._process.pid).create_time()
        except psutil.NoSuchProcess:
            self._create_time = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def create_time(self) -> float | None:
        return self._create_time

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate_tree(self, *, timeout: float) -> None:
        if self._process is None:
            return
        pid = self._process.pid
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            try:
                parent.terminate()
            except psutil.NoSuchProcess:
                pass
            targets = children + [parent]
            _, alive = psutil.wait_procs(targets, timeout=timeout)
            for proc in alive:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=timeout)
        except psutil.NoSuchProcess:
            pass


class RealWorkerProcessManager:
    """Spawns and polls real worker processes for the ProcessSupervisor."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        fingerprints: dict[WorkerName, EngineFingerprint],
        jobs_root: Path,
        instance_id: str,
        logs_root: Path,
        startup_timeout_seconds: float = 120.0,
    ) -> None:
        self._settings = settings
        self._fingerprints = fingerprints
        self._jobs_root = jobs_root.resolve()
        self._instance_id = instance_id
        self._logs_root = logs_root
        self._startup_timeout_seconds = startup_timeout_seconds
        self._managed: dict[WorkerName, ManagedProcess] = {}
        self._identity: dict[WorkerName, EngineIdentity] = {}

    def running_engine(self, engine: WorkerName) -> bool:
        proc = self._managed.get(engine)
        return proc is not None and proc.is_alive()

    def engine_identity(self, engine: WorkerName) -> EngineIdentity | None:
        return self._identity.get(engine)

    async def start_engine(self, engine: WorkerName) -> None:
        if engine == "indextts":
            args, cwd = self._index_args()
        else:
            args, cwd = self._gsv_args()
        log_path = self._logs_root / f"{engine}.stdout.log"
        err_path = self._logs_root / f"{engine}.stderr.log"
        proc = ManagedProcess(
            role=engine,
            args=args,
            cwd=cwd,
            stdout_log=log_path,
            stderr_log=err_path,
        )
        proc.start()
        self._managed[engine] = proc
        try:
            await self._wait_ready(engine, proc)
        except Exception:
            proc.terminate_tree(timeout=5.0)
            self._managed.pop(engine, None)
            raise
        identity = EngineIdentity(
            worker=engine,
            pid=proc.pid or 0,
            create_time=proc.create_time or 0.0,
            python_executable=Path(sys.executable),
            fingerprint=self._fingerprints[engine],
        )
        self._identity[engine] = identity

    async def _wait_ready(self, engine: WorkerName, proc: ManagedProcess) -> None:
        import httpx

        base_url = self._engine_base_url(engine)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout_seconds
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                if not proc.is_alive():
                    raise PipelineError(
                        ErrorCode.ENGINE_UNAVAILABLE,
                        "runtime",
                        f"{engine} worker exited during startup",
                        retryable=False,
                    )
                if await self._probe_ready(engine, client, proc):
                    return
                await asyncio.sleep(0.25)
        raise PipelineError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "runtime",
            f"{engine} worker did not become ready in time",
            retryable=False,
        )

    async def _probe_ready(self, engine: WorkerName, client: Any, proc: ManagedProcess) -> bool:
        if engine == "indextts":
            try:
                resp = await client.get("/health/ready")
                return bool(resp.status_code == 200)
            except Exception:
                return False
        # GPT-SoVITS official api_v2 has no health route: /docs 200 plus the
        # loopback port LISTEN socket owner must be the recorded PID.
        try:
            resp = await client.get("/docs")
            if resp.status_code != 200:
                return False
        except Exception:
            return False
        try:
            port = self._gsv_port()
            for conn in psutil.net_connections(kind="tcp"):
                laddr = conn.laddr
                if isinstance(laddr, tuple) and len(laddr) == 2:
                    ip, conn_port = laddr
                    if (
                        int(conn_port) == port
                        and str(ip) in ("127.0.0.1", "::1")
                        and conn.status == "LISTEN"
                    ):
                        return conn.pid == proc.pid
        except (psutil.AccessDenied, psutil.Error):
            return False
        return False

    def _gsv_port(self) -> int:
        parsed = urlparse(self._settings.engines.gpt_sovits.base_url)
        return parsed.port or 9880

    async def stop_engine(self, engine: WorkerName, *, deadline: float | None = None) -> None:
        proc = self._managed.pop(engine, None)
        self._identity.pop(engine, None)
        if proc is None:
            return
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + 5.0
        await self._graceful_stop(engine)
        if proc.is_alive():
            remaining = max(0.0, deadline - loop.time())
            proc.terminate_tree(timeout=remaining)

    async def _graceful_stop(self, engine: WorkerName) -> None:
        import httpx

        base_url = self._engine_base_url(engine)
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=3.0) as client:
                if engine == "indextts":
                    await client.post("/v1/control/stop")
                else:
                    await client.get("/control", params={"command": "exit"})
        except Exception:
            pass

    def _engine_base_url(self, engine: WorkerName) -> str:
        if engine == "indextts":
            return self._settings.engines.indextts.base_url
        return self._settings.engines.gpt_sovits.base_url

    def _index_args(self) -> tuple[list[str], Path]:
        python = self._settings.engines.indextts.python_executable
        repo_dir = self._settings.engines.indextts.repo_dir
        args = [
            str(python),
            "-m",
            "workers.indextts2",
            "--host",
            "127.0.0.1",
            "--port",
            "9871",
            "--repo-dir",
            str(repo_dir),
            "--model-dir",
            str(repo_dir / "checkpoints"),
            "--aux-root",
            str(repo_dir / "checkpoints" / "hf_cache" / "pinned"),
            "--jobs-root",
            str(self._jobs_root),
            "--engine-lock",
            str(self._settings.engine_lock_path),
            "--checkpoint-lock",
            str(self._settings.checkpoint_lock_path),
            "--environment-lock",
            str(
                self._settings.runtime_dir.parent
                / "config"
                / "env-locks"
                / "index-pip-requirements.lock.txt"
            ),
            "--environment-freeze",
            str(
                self._settings.runtime_dir.parent / "config" / "env-locks" / "index-pip-freeze.txt"
            ),
            "--expected-fingerprint-json",
            self._fingerprints["indextts"].model_dump_json(),
            "--device",
            "cuda:0",
            "--fp16",
        ]
        cwd = self._settings.runtime_dir.parent  # project root D:\TTSsystem
        return args, cwd

    def _gsv_args(self) -> tuple[list[str], Path]:
        python = self._settings.engines.gpt_sovits.python_executable
        repo_dir = self._settings.engines.gpt_sovits.repo_dir
        args = [
            str(python),
            str(repo_dir / "api_v2.py"),
            "-a",
            "127.0.0.1",
            "-p",
            "9880",
            "-c",
            str(repo_dir / "GPT_SoVITS" / "configs" / "tts_infer.yaml"),
        ]
        return args, repo_dir
