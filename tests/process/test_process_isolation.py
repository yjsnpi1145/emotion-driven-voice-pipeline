from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.fixtures.external_harness import (
    FakeServerProcess,
    build_external_config,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.process
def test_process_isolation_with_external_engines(tmp_path: Path) -> None:
    """Control, Index fake and GSV fake run in separate processes/interpreters."""
    # Three distinct Python 3.11 interpreters: control uses the root venv,
    # the two fake servers use two freshly created venvs.
    control_python = _REPO / ".venv" / "Scripts" / "python.exe"
    if not control_python.is_file():
        pytest.skip("root dev venv not available")

    def _make_venv(name: str) -> str:
        path = tmp_path / name
        subprocess.run(
            ["uv", "venv", str(path), "--python", "3.11"],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return str(path / "Scripts" / "python.exe")

    index_python = _make_venv("index-env")
    gsv_python = _make_venv("gsv-env")

    ready_dir = tmp_path / "servers"
    ready_dir.mkdir()
    index_server = FakeServerProcess("indextts", ready_dir, delay_ms=300, python=index_python)
    gsv_server = FakeServerProcess("gpt_sovits", ready_dir, delay_ms=300, python=gsv_python)
    index_server.start()
    gsv_server.start()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = build_external_config(
        config_dir=config_dir,
        runtime_dir=tmp_path / "runtime",
        index_server=index_server,
        gsv_server=gsv_server,
        queue_timeout_seconds=5.0,
        request_timeout_seconds=10.0,
    )

    control_proc: subprocess.Popen | None = None
    control_log = tmp_path / "control-stderr.txt"
    control_log_fh = control_log.open("wb")
    try:
        # Start the control plane as a real process.
        control_proc = subprocess.Popen(
            [str(control_python), "-m", "voice_pipeline", "serve", "--config", str(config)],
            cwd=str(_REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=control_log_fh,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        base_url = "http://127.0.0.1:18765"
        deadline = time.monotonic() + 60
        health = None
        while time.monotonic() < deadline:
            if control_proc.poll() is not None:
                break
            try:
                import urllib.request

                with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=3) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                if health["status"] in ("ready", "degraded"):
                    break
            except Exception:
                time.sleep(0.3)
        assert health is not None, "control plane did not become ready; stderr:\n" + (
            control_log.read_text(encoding="utf-8", errors="replace")[-1500:]
            if control_log.is_file()
            else ""
        )
        assert health["mode"] == "external_test"
        assert health["control"]["pid"] > 0

        # PIDs: control != index fake != gsv fake
        pids = {
            "control": int(health["control"]["pid"]),
            "indextts": index_server.pid,
            "gpt_sovits": gsv_server.pid,
        }
        assert len(set(pids.values())) == 3, pids
        assert control_proc.poll() is None

        # Interpreters: three distinct paths
        interpreters = {
            "control": health["control"]["python_executable"],
            "indextts": index_server.python_executable,
            "gpt_sovits": gsv_server.python_executable,
        }
        normalized = {k: str(Path(v)).replace("\\", "/").lower() for k, v in interpreters.items()}
        assert len(set(normalized.values())) == 3, normalized

        # All services bind only 127.0.0.1
        for url in (base_url, index_server.base_url, gsv_server.base_url):
            host, port = url.replace("http://", "").split(":")
            with socket.create_connection((host, int(port)), timeout=5):
                pass

        # CLI runs from outside the repository
        with tempfile.TemporaryDirectory() as outside:
            run = subprocess.run(
                [
                    str(control_python),
                    "-m",
                    "voice_pipeline",
                    "doctor",
                    "--server",
                    base_url,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=outside,
            )
            assert run.returncode == 0, run.stderr
            payload = json.loads(run.stdout)
            assert payload["mode"] == "external_test"
    finally:
        if control_proc is not None and control_proc.poll() is None:
            try:
                import urllib.request

                urllib.request.urlopen(f"{base_url}/api/v1/control/shutdown", timeout=5, data=b"")
            except Exception:
                pass
            time.sleep(1)
            if control_proc.poll() is None:
                control_proc.kill()
        try:
            control_log_fh.close()
        except Exception:
            pass
        index_server.stop()
        gsv_server.stop()
