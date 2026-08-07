"""Shared helpers for cross-process external_test integration tests."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from voice_pipeline.api.dependencies import fingerprint_from_challenge

_FIXTURE_DIR = Path(__file__).resolve().parent
_REPO = _FIXTURE_DIR.parents[2]
# The venv launcher spawns a child interpreter and exits, which breaks
# Popen-based liveness checks; use the real base interpreter instead.
_BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)

_CHALLENGES = {"indextts": "external-test-index", "gpt_sovits": "external-test-gsv"}


class FakeServerProcess:
    """Manages one fake engine server in a separate Python interpreter."""

    def __init__(
        self,
        engine: str,
        ready_dir: Path,
        delay_ms: int = 450,
        python: str | None = None,
    ):
        self.engine = engine
        self.ready_file = ready_dir / f"{engine}-ready.json"
        self.audit_log = ready_dir / f"{engine}-audit.jsonl"
        self.delay_ms = delay_ms
        self.python = python or _BASE_PYTHON
        self.fingerprint = fingerprint_from_challenge(
            engine,
            _CHALLENGES[engine],  # type: ignore[arg-type]
        )
        self._process: subprocess.Popen | None = None
        self._info: dict[str, Any] | None = None

    def start(self) -> None:
        self.ready_file.unlink(missing_ok=True)
        self.audit_log.unlink(missing_ok=True)
        self.stderr_log = self.ready_file.parent / f"{self.engine}-stderr.txt"
        self.stderr_log.unlink(missing_ok=True)
        self._stderr_fh = self.stderr_log.open("wb")
        args = [
            self.python,
            str(_FIXTURE_DIR / "fake_engine_server.py"),
            "--engine",
            self.engine,
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--ready-file",
            str(self.ready_file),
            "--expected-fingerprint-json",
            json.dumps(self.fingerprint.model_dump(mode="json")),
            "--delay-ms",
            str(self.delay_ms),
            "--audit-log",
            str(self.audit_log),
        ]
        self._process = subprocess.Popen(
            args,
            cwd=str(_REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_fh,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.ready_file.is_file():
                self._info = json.loads(self.ready_file.read_text(encoding="utf-8"))
                return
            if self._process.poll() is not None:
                detail = ""
                if self.stderr_log.is_file():
                    detail = self.stderr_log.read_text(encoding="utf-8")[-500:]
                raise RuntimeError(f"{self.engine} fake server exited early: {detail}")
            time.sleep(0.05)
        raise TimeoutError(f"{self.engine} fake server did not become ready")

    @property
    def base_url(self) -> str:
        assert self._info is not None
        return f"http://127.0.0.1:{self._info['port']}"

    @property
    def pid(self) -> int:
        assert self._info is not None
        return int(self._info["pid"])

    @property
    def python_executable(self) -> str:
        assert self._info is not None
        return self._info["sys.executable"]

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if getattr(self, "_stderr_fh", None) is not None:
            try:
                self._stderr_fh.close()
            except Exception:
                pass

    def audit_rows(self) -> list[dict[str, Any]]:
        if not self.audit_log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.audit_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def build_external_config(
    *,
    config_dir: Path,
    runtime_dir: Path,
    index_server: FakeServerProcess,
    gsv_server: FakeServerProcess,
    queue_timeout_seconds: float = 5.0,
    request_timeout_seconds: float = 10.0,
) -> Path:
    config = config_dir / "app.external-test.yaml"
    config.write_text(
        f"""
schema_version: 1
mode: external_test
engine_lifecycle: resident
server: {{host: 127.0.0.1, port: 18765}}
runtime_dir: {str(runtime_dir).replace(chr(92), "/")}
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {{max_concurrency: 1, queue_timeout_seconds: {queue_timeout_seconds}}}
engines:
  indextts:
    base_url: {index_server.base_url}
    python_executable: {index_server.python_executable}
    repo_dir: repo-index
    request_timeout_seconds: {request_timeout_seconds}
    expected_fingerprint: {{challenge: external-test-index}}
  gpt_sovits:
    base_url: {gsv_server.base_url}
    python_executable: {gsv_server.python_executable}
    repo_dir: repo-gsv
    request_timeout_seconds: {request_timeout_seconds}
    expected_fingerprint: {{challenge: external-test-gsv}}
""",
        encoding="utf-8",
    )
    return config
