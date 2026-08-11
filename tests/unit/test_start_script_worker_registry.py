from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import psutil

_REPO = Path(__file__).resolve().parents[2]
_HELPER = _REPO / "scripts" / "process-registry.ps1"


def _archive_registry(tmp_path: Path, worker_pid: int, worker_create_time: float) -> dict:
    run_file = tmp_path / "processes.json"
    run_file.write_text(
        json.dumps(
            {
                "control": {"pid": 999_999, "create_time": 1.0},
                "workers": {
                    "indextts": {"pid": None, "create_time": None},
                    "gpt_sovits": {
                        "pid": worker_pid,
                        "create_time": worker_create_time,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    command = (
        f". '{_HELPER}'; "
        f"$run = Get-Content -LiteralPath '{run_file}' -Raw | ConvertFrom-Json; "
        f"Move-StaleProcessRegistry -RunFile '{run_file}' -RunPayload $run | "
        "ConvertTo-Json -Depth 5 -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(completed.stdout)


def test_stale_registry_with_live_recorded_worker_is_archived_as_orphaned(
    tmp_path: Path,
) -> None:
    process = psutil.Process(os.getpid())

    result = _archive_registry(tmp_path, process.pid, process.create_time())

    assert result["classification"] == "orphaned"
    assert result["live_worker_pids"] == [process.pid]
    assert Path(result["archive_path"]).is_file()
    assert ".orphaned." in Path(result["archive_path"]).name
    assert not (tmp_path / "processes.json").exists()


def test_stale_registry_without_live_recorded_worker_is_archived_as_stale(tmp_path: Path) -> None:
    result = _archive_registry(tmp_path, 999_998, 1.0)

    assert result["classification"] == "stale"
    assert result["live_worker_pids"] == []
    assert Path(result["archive_path"]).is_file()
    assert ".stale." in Path(result["archive_path"]).name
