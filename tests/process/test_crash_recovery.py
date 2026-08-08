from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import httpx
import psutil
import pytest

from tests.fixtures.external_harness import FakeServerProcess, build_external_config
from tests.integration_cpu.conftest import write_tone

_REPO = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base_url: str, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.is_file()
                else ""
            )
            raise AssertionError(f"control process exited before readiness:\n{stderr[-2000:]}")
        try:
            response = httpx.get(f"{base_url}/api/v1/health", timeout=2)
            if response.status_code == 200 and response.json()["status"] in {"ready", "degraded"}:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("control process did not become ready")


def _wait_job(base_url: str, job_id: str, *, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = httpx.get(f"{base_url}/api/v1/jobs/{job_id}", timeout=2)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] == expected:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach {expected}")


def _wait_engine_idle(base_url: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = httpx.get(f"{base_url}/__control/status", timeout=2).json()
        if status["active_inference"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("external engine never became idle")


@pytest.mark.process
def test_hard_killed_control_marks_running_job_interrupted_and_allows_frozen_retry(
    tmp_path: Path,
) -> None:
    """A restart never resumes a dead process's running DB claim in place."""
    control_python = _REPO / ".venv" / "Scripts" / "python.exe"
    if not control_python.is_file():
        pytest.skip("root dev venv not available")
    ready_dir = tmp_path / "engines"
    ready_dir.mkdir()
    index = FakeServerProcess("indextts", ready_dir, delay_ms=2500)
    gsv = FakeServerProcess("gpt_sovits", ready_dir, delay_ms=200)
    index.start()
    gsv.start()
    base_url = f"http://127.0.0.1:{_free_port()}"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = build_external_config(
        config_dir=config_dir,
        runtime_dir=tmp_path / "runtime",
        index_server=index,
        gsv_server=gsv,
        server_port=int(base_url.rsplit(":", 1)[1]),
        queue_timeout_seconds=20,
        request_timeout_seconds=10,
    )
    first: subprocess.Popen[bytes] | None = None
    second: subprocess.Popen[bytes] | None = None
    control_log = tmp_path / "control.stderr.log"
    control_log_fh = control_log.open("wb")
    try:
        first = subprocess.Popen(
            [str(control_python), "-m", "voice_pipeline", "serve", "--config", str(config)],
            cwd=str(_REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=control_log_fh,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _wait_ready(base_url, first, control_log)
        base_voice = tmp_path / "base.wav"
        write_tone(base_voice, seconds=5.0)
        submitted = httpx.post(
            f"{base_url}/api/v1/jobs/reference",
            json={
                "request_id": "7a7f8b25-c247-4495-8f34-09f741dbed7a",
                "base_voice_path": str(base_voice.resolve()),
                "ref_text_cn": "我仍然活着。",
                "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
                "seed": 1234,
            },
            timeout=5,
        )
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        _wait_job(base_url, job_id, expected="running")
        assert httpx.get(f"{index.base_url}/__control/status", timeout=2).json()[
            "active_inference"
        ] == 1

        psutil.Process(first.pid).kill()
        first.wait(timeout=10)
        _wait_engine_idle(index.base_url)

        second = subprocess.Popen(
            [str(control_python), "-m", "voice_pipeline", "serve", "--config", str(config)],
            cwd=str(_REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=control_log_fh,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _wait_ready(base_url, second, control_log)
        interrupted = _wait_job(base_url, job_id, expected="interrupted")
        assert interrupted["error"]["stage"] == "recovery"  # type: ignore[index]

        retried = httpx.post(
            f"{base_url}/api/v1/jobs/{job_id}/retry",
            json={"mode": "frozen_snapshot"},
            timeout=5,
        )
        assert retried.status_code == 202, retried.text
        _wait_job(base_url, retried.json()["job_id"], expected="succeeded")
        assert httpx.get(f"{index.base_url}/__control/status", timeout=2).json()[
            "max_active_observed"
        ] == 1
    finally:
        for process in (second, first):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        index.stop()
        gsv.stop()
        control_log_fh.close()
