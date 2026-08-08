from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.integration_cpu.conftest import write_tone

_REQUIRED_RECEIPT_FIELDS = [
    "schema_version",
    "instance_id",
    "run_file",
    "started_at_utc",
    "finished_at_utc",
    "elapsed_seconds",
    "shutdown_http",
    "observed_processes",
    "job_counts",
    "run_file_deleted",
    "status",
]


@dataclass
class PwshResult:
    returncode: int
    stdout: str
    stderr: str


def validate_receipt(receipt: dict[str, Any]) -> list[str]:
    errors = []
    for field in _REQUIRED_RECEIPT_FIELDS:
        if field not in receipt:
            errors.append(f"missing field: {field}")
    if errors:
        return errors
    if receipt["schema_version"] != 1:
        errors.append("schema_version != 1")
    if receipt["elapsed_seconds"] < 0 or receipt["elapsed_seconds"] > 10:
        errors.append("elapsed_seconds out of 0..10")
    if not isinstance(receipt["observed_processes"], list):
        errors.append("observed_processes not a list")
    expected_counts = {"queued", "running", "interrupted"}
    if set((receipt.get("job_counts") or {}).keys()) != expected_counts:
        errors.append("job_counts schema invalid")
    if receipt["status"] != "stopped":
        errors.append("status != stopped")
    if receipt["run_file_deleted"] is not True:
        errors.append("run_file_deleted != true")
    return errors


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the first top-level JSON object embedded in the output."""
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx < 0 or end_idx < start_idx:
        raise ValueError(f"no JSON object found in output: {text!r}")
    return json.loads(text[start_idx : end_idx + 1])


def _run_pwsh(script: Path, args: list[str]) -> PwshResult:
    """Run a PowerShell script writing output to files.

    ``capture_output=True`` is deliberately avoided: a long-lived child
    process (the control plane) inherits the stdout/stderr handles, which
    would block ``communicate()`` forever on Windows and would also hold the
    temporary files open.
    """
    tmp = Path(tempfile.gettempdir()) / f"voice-pipeline-pwsh-{time.time_ns()}"
    tmp.mkdir(parents=True)
    out_path = tmp / "stdout.txt"
    err_path = tmp / "stderr.txt"
    with (
        out_path.open("w", encoding="utf-8") as out_fh,
        err_path.open("w", encoding="utf-8") as err_fh,
    ):
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(script)] + args,
            stdout=out_fh,
            stderr=err_fh,
            timeout=180,
            cwd=str(script.parent.parent),
        )
    return PwshResult(
        returncode=proc.returncode,
        stdout=out_path.read_text(encoding="utf-8"),
        stderr=err_path.read_text(encoding="utf-8"),
    )


def _cleanup_leftover_control(repo: Path) -> None:
    """Kill any leftover test control processes and remove stale run files."""
    import psutil

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] == "python.exe":
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "voice_pipeline" in cmdline and "serve" in cmdline:
                    proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    (repo / "runtime" / "run" / "processes.json").unlink(missing_ok=True)


@pytest.mark.process
def test_start_stop_scripts_full_cycle(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    python = repo / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        pytest.skip("root dev venv not available")
    _cleanup_leftover_control(repo)

    # fake-mode config
    config = tmp_path / "app.fake.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server: {{host: 127.0.0.1, port: 8765}}
runtime_dir: {runtime_dir}
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {{max_concurrency: 1, queue_timeout_seconds: 10}}
engines:
  indextts:
    base_url: http://127.0.0.1:19871
    python_executable: i.exe
    repo_dir: i
    request_timeout_seconds: 10
  gpt_sovits:
    base_url: http://127.0.0.1:19880
    python_executable: g.exe
    repo_dir: g
    request_timeout_seconds: 10
""".format(runtime_dir=str(tmp_path / "runtime").replace("\\", "/")),
        encoding="utf-8",
    )

    start = _run_pwsh(
        repo / "scripts" / "start.ps1",
        ["-Config", str(config), "-PythonExecutable", str(python), "-Json"],
    )
    assert start.returncode == 0, start.stderr
    start_info = _extract_json(start.stdout)
    assert start_info["control_pid"] > 0
    assert start_info["instance_id"]
    run_file = Path(start_info["run_file"])

    try:
        # health via CLI doctor
        doctor = subprocess.run(
            [
                str(python),
                "-m",
                "voice_pipeline",
                "doctor",
                "--server",
                "http://127.0.0.1:8765",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo),
        )
        assert doctor.returncode == 0, doctor.stderr
        health = json.loads(doctor.stdout)
        assert health["status"] == "ready"

        # one end-to-end segment job
        base = tmp_path / "base.wav"
        write_tone(base, 5.0)
        request = tmp_path / "request.json"
        request.write_text(
            json.dumps(
                {
                    "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
                    "base_voice_path": str(base.resolve()),
                    "ref_text_cn": "我已经失去了一切，可我仍然活着。",
                    "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
                    "target_text": "私はまだ生きている。",
                    "target_language": "ja",
                    "seed": 1234,
                }
            ),
            encoding="utf-8",
        )
        synth = subprocess.run(
            [
                str(python),
                "-m",
                "voice_pipeline",
                "synthesize-segment",
                "--server",
                "http://127.0.0.1:8765",
                "--request",
                str(request),
                "--output-dir",
                str(tmp_path / "out"),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo),
        )
        assert synth.returncode == 0, synth.stderr
        result = json.loads(synth.stdout)
        assert result["status"] == "succeeded"
        assert (tmp_path / "out" / "target.wav").exists()
    finally:
        receipt_path = tmp_path / "stop-receipt.json"
        stop = _run_pwsh(
            repo / "scripts" / "stop.ps1",
            ["-RunFile", str(run_file), "-ReceiptPath", str(receipt_path), "-Json"],
        )
        assert stop.returncode == 0, stop.stderr
        receipt = _extract_json(stop.stdout)
        errors = validate_receipt(receipt)
        assert not errors, errors
        assert not run_file.exists()
        assert not json.loads(receipt_path.read_text(encoding="utf-8"))["status"] != "stopped"
        _cleanup_leftover_control(repo)

    # control process must be gone
    time.sleep(0.5)


@pytest.mark.process
def test_receipt_validation_rejects_bad_samples(tmp_path: Path) -> None:
    good = {
        "schema_version": 1,
        "instance_id": "10676aa6-86e1-424d-a8dd-77f6ce09fc57",
        "run_file": r"D:\runtime\run\processes.json",
        "started_at_utc": "2026-08-07T00:00:00Z",
        "finished_at_utc": "2026-08-07T00:00:01Z",
        "elapsed_seconds": 1.0,
        "shutdown_http": {
            "attempted": True,
            "timeout_seconds": 6,
            "outcome": "completed",
            "status_code": 200,
        },
        "observed_processes": [
            {
                "role": "control",
                "pid": 123,
                "create_time": 1.0,
                "parent_pid": None,
                "stop_method": "graceful",
                "verified_exited": True,
                "verified_at_utc": "2026-08-07T00:00:01Z",
            }
        ],
        "job_counts": {"queued": 0, "running": 0, "interrupted": 0},
        "run_file_deleted": True,
        "status": "stopped",
    }
    assert validate_receipt(dict(good)) == []

    bad_elapsed = dict(good)
    bad_elapsed["elapsed_seconds"] = 11.0
    assert validate_receipt(bad_elapsed)

    bad_status = dict(good)
    bad_status["status"] = "failed"
    assert validate_receipt(bad_status)

    missing = {k: v for k, v in good.items() if k != "status"}
    assert validate_receipt(missing)
