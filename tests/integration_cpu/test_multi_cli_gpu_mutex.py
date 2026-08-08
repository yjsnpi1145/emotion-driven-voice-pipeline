from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from tests.fixtures.external_harness import (
    FakeServerProcess,
    build_external_config,
)
from tests.integration_cpu.conftest import write_tone

_REPO = Path(__file__).resolve().parents[2]


def _interval_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


@pytest.mark.integration_cpu
def test_multi_cli_gpu_mutex_with_twelve_clients(tmp_path: Path) -> None:
    control_python = _REPO / ".venv" / "Scripts" / "python.exe"
    if not control_python.is_file():
        pytest.skip("root dev venv not available")

    ready_dir = tmp_path / "servers"
    ready_dir.mkdir()
    index_server = FakeServerProcess("indextts", ready_dir, delay_ms=450)
    gsv_server = FakeServerProcess("gpt_sovits", ready_dir, delay_ms=450)
    index_server.start()
    gsv_server.start()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        control_port = int(probe.getsockname()[1])
    config = build_external_config(
        config_dir=config_dir,
        runtime_dir=tmp_path / "runtime",
        index_server=index_server,
        gsv_server=gsv_server,
        server_port=control_port,
        queue_timeout_seconds=30.0,
        request_timeout_seconds=15.0,
    )

    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)

    control_proc: subprocess.Popen | None = None
    control_log = tmp_path / "control-stderr.txt"
    control_log_fh = control_log.open("wb")
    try:
        control_proc = subprocess.Popen(
            [str(control_python), "-m", "voice_pipeline", "serve", "--config", str(config)],
            cwd=str(_REPO),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=control_log_fh,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        base_url = f"http://127.0.0.1:{control_port}"
        import urllib.request

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if control_proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=3) as resp:
                    health = json.loads(resp.read().decode("utf-8"))
                if health["status"] in ("ready", "degraded"):
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise AssertionError(
                "control plane did not become ready; stderr:\n"
                + (
                    control_log.read_text(encoding="utf-8", errors="replace")[-1500:]
                    if control_log.is_file()
                    else ""
                )
            )

        # Build the request payloads for reference, GSV and compound segment jobs.
        import uuid

        _EMOTION = [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20]

        def _request(kind: str, idx: int) -> dict:
            rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mutex-{kind}-{idx}"))
            if kind == "reference":
                return {
                    "request_id": rid,
                    "base_voice_path": str(base_voice.resolve()),
                    "ref_text_cn": "我已经失去了一切，可我仍然活着。",
                    "emotion_vector": _EMOTION,
                }
            if kind == "gsv":
                return {
                    "request_id": rid,
                    "reference_manifest_path": str(
                        tmp_path / f"out-ref-{idx}.reference-manifest.json"
                    ),
                    "target_text": f"私はまだ生きている {idx}。",
                    "target_language": "ja",
                    "seed": 100 + idx,
                }
            return {
                "request_id": rid,
                "base_voice_path": str(base_voice.resolve()),
                "ref_text_cn": "我已经失去了一切，可我仍然活着。",
                "emotion_vector": _EMOTION,
                "target_text": f"私はまだ生きている {idx}。",
                "target_language": "ja",
                "seed": 100 + idx,
            }

        # Pre-create four reference jobs via CLI; this also writes the
        # portable reference manifests consumed by the GSV jobs.
        ref_manifest_paths = []
        for idx in range(4):
            payload = _request("reference", idx)
            req_path = tmp_path / f"reference-{idx}.json"
            req_path.write_text(json.dumps(payload), encoding="utf-8")
            run = subprocess.run(
                [
                    str(control_python),
                    "-m",
                    "voice_pipeline",
                    "generate-reference",
                    "--server",
                    base_url,
                    "--request",
                    str(req_path),
                    "--output",
                    str(tmp_path / f"out-ref-{idx}.wav"),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(_REPO),
            )
            assert run.returncode == 0, run.stdout + run.stderr
            ref_manifest_paths.append(str(tmp_path / f"out-ref-{idx}.reference-manifest.json"))

        assert all(Path(p).is_file() for p in ref_manifest_paths)

        # Build twelve command specs (4 reference, 4 GSV, 4 segment).
        commands = []
        for idx in range(4):
            for kind in ("reference", "gsv", "segment"):
                payload = _request(kind, idx)
                req_path = tmp_path / f"{kind}-{idx}.json"
                req_path.write_text(json.dumps(payload), encoding="utf-8")
                if kind == "reference":
                    cmd = [
                        "generate-reference",
                        "--output",
                        str(tmp_path / f"out-ref-run-{idx}.wav"),
                    ]
                elif kind == "gsv":
                    cmd = ["generate-gsv", "--output", str(tmp_path / f"out-gsv-{idx}.wav")]
                else:
                    cmd = ["synthesize-segment", "--output-dir", str(tmp_path / f"out-seg-{idx}")]
                commands.append((kind, idx, req_path, cmd))

        # Launch all twelve CLI processes concurrently.
        procs = []
        for _kind, _idx, req_path, cmd in commands:
            argv = (
                [str(control_python), "-m", "voice_pipeline"]
                + cmd
                + [
                    "--server",
                    base_url,
                    "--request",
                    str(req_path),
                    "--json",
                ]
            )
            procs.append(
                subprocess.Popen(
                    argv,
                    cwd=str(_REPO),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            )
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            results.append((proc.returncode, out, err))

        # All must succeed (queue serializes; failures must not wedge).
        failures = [r for r in results if r[0] != 0]
        assert not failures, failures

        # Health: max_active_observed == 1 and no residue.
        with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["gpu_queue"]["max_active_observed"] == 1
        assert health["gpu_queue"]["active_count"] == 0
        assert health["gpu_queue"]["queued_count"] == 0

        for server in (index_server, gsv_server):
            with urllib.request.urlopen(f"{server.base_url}/__control/status", timeout=5) as resp:
                engine_status = json.loads(resp.read().decode("utf-8"))
            assert engine_status["active_inference"] == 0
            assert engine_status["max_active_observed"] == 1

        db_path = tmp_path / "runtime" / "state" / "pipeline.sqlite3"
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        # GPU intervals from audit logs must be pairwise non-overlapping.
        intervals = []
        for server in (index_server, gsv_server):
            for row in server.audit_rows():
                if "monotonic_enter" in row and "monotonic_exit" in row:
                    intervals.append((row["monotonic_enter"], row["monotonic_exit"]))
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                assert not _interval_overlap(intervals[i], intervals[j]), (
                    f"overlapping GPU intervals: {intervals[i]} vs {intervals[j]}"
                )
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
