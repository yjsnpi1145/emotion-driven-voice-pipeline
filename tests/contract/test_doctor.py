from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from voice_pipeline.runtime.doctor import build_doctor_payload, validate_doctor_payload
from voice_pipeline.runtime.fingerprints import sha256_file


def test_doctor_rejects_any_shared_interpreter(doctor_payload) -> None:
    doctor_payload["control"]["python_executable"] = "D:/same/python.exe"  # type: ignore[index]
    doctor_payload["workers"]["indextts"]["python_executable"] = "D:/same/python.exe"  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "ENVIRONMENTS_NOT_ISOLATED" in result.codes


def test_sha256_file_changes_when_weight_changes(tmp_path: Path) -> None:
    weight = tmp_path / "model.pth"
    weight.write_bytes(b"a")
    first = sha256_file(weight)
    weight.write_bytes(b"b")
    assert sha256_file(weight) != first


def test_exclusive_doctor_accepts_expected_stopped_worker(doctor_payload) -> None:
    doctor_payload["engine_lifecycle"] = "exclusive_process"  # type: ignore[index]
    doctor_payload["workers"]["indextts"]["state"] = "ready"  # type: ignore[index]
    doctor_payload["workers"]["gpt_sovits"]["state"] = "stopped_expected"  # type: ignore[index]
    assert validate_doctor_payload(doctor_payload).status == "ready"  # type: ignore[arg-type]


def test_exclusive_doctor_rejects_two_ready_workers(doctor_payload) -> None:
    doctor_payload["engine_lifecycle"] = "exclusive_process"  # type: ignore[index]
    doctor_payload["workers"]["indextts"]["state"] = "ready"  # type: ignore[index]
    doctor_payload["workers"]["gpt_sovits"]["state"] = "ready"  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "EXCLUSIVE_WORKER_STATE_INVALID" in result.codes


def test_unknown_lifecycle_fails(doctor_payload) -> None:
    doctor_payload["engine_lifecycle"] = "bogus"  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "LIFECYCLE_UNKNOWN" in result.codes


def test_resident_requires_both_workers_ready(doctor_payload) -> None:
    doctor_payload["engine_lifecycle"] = "resident"  # type: ignore[index]
    doctor_payload["workers"]["gpt_sovits"]["state"] = "stopped_expected"  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "RESIDENT_WORKER_NOT_READY" in result.codes


def test_checkpoint_digest_mismatch_fails(doctor_payload) -> None:
    doctor_payload["workers"]["indextts"]["digest_mismatch"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "CHECKPOINT_DIGEST_MISMATCH" in result.codes


def test_uv_lock_mismatch_fails(doctor_payload) -> None:
    doctor_payload["uv_lock_mismatch"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "UV_LOCK_MISMATCH" in result.codes


def test_env_lock_mismatch_fails(doctor_payload) -> None:
    doctor_payload["env_lock_mismatch"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "ENV_LOCK_MISMATCH" in result.codes


def test_inventory_mismatch_fails(doctor_payload) -> None:
    doctor_payload["inventory_mismatch"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "INVENTORY_MISMATCH" in result.codes


def test_pid_registry_stale_fails(doctor_payload) -> None:
    doctor_payload["pid_registry_stale"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "PID_REGISTRY_STALE" in result.codes


def test_queue_concurrency_must_be_one(doctor_payload) -> None:
    doctor_payload["gpu_queue"]["max_concurrency"] = 2  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "QUEUE_CONCURRENCY_INVALID" in result.codes


def test_real_mode_requires_cuda(doctor_payload) -> None:
    doctor_payload["cuda"]["available"] = False  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "CUDA_UNAVAILABLE" in result.codes


def test_model_revision_mismatch_fails(doctor_payload) -> None:
    doctor_payload["model_revision_mismatch"] = True  # type: ignore[index]
    result = validate_doctor_payload(doctor_payload)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert "MODEL_REVISION_MISMATCH" in result.codes


def test_valid_payload_is_ready(doctor_payload) -> None:
    assert validate_doctor_payload(doctor_payload).status == "ready"  # type: ignore[arg-type]


def test_build_doctor_payload_round_trips(tmp_path: Path) -> None:
    from voice_pipeline.core.config import load_settings

    # Reuse the valid doctor_payload as the health shape, wrapped in
    # namespaces that build_doctor_payload expects.
    health = SimpleNamespace(
        workers=SimpleNamespace(
            indextts=SimpleNamespace(
                state="ready",
                pid=1001,
                create_time=100.0,
                python_executable=r"D:\envs\index\python.exe",
                python_version="3.11.15",
                source_revision="90ca4d608209584bad3a5bd5becc0b80c146e60f",
                active_inference=0,
            ),
            gpt_sovits=SimpleNamespace(
                state="ready",
                pid=1002,
                create_time=200.0,
                python_executable=r"D:\envs\gsv\python.exe",
                python_version="3.11.15",
                source_revision="d523079fc05d9a8028d6085bffe4a2757c32abb6",
                active_inference=0,
            ),
        )
    )
    queue_stats = SimpleNamespace(
        state="accepting",
        active_count=0,
        queued_count=0,
        max_active_observed=1,
        max_concurrency=1,
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "app.yaml"
    config.write_text(
        """
schema_version: 1
mode: external_test
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 5.0}
engines:
  indextts:
    base_url: http://127.0.0.1:9
    python_executable: D:/envs/index/python.exe
    repo_dir: repo-index
    request_timeout_seconds: 4.0
    expected_fingerprint: {challenge: external-test-index}
  gpt_sovits:
    base_url: http://127.0.0.1:9
    python_executable: D:/envs/gsv/python.exe
    repo_dir: repo-gsv
    request_timeout_seconds: 4.0
    expected_fingerprint: {challenge: external-test-gsv}
""",
        encoding="utf-8",
    )
    settings = load_settings(config)
    audit_log = tmp_path / "engine-audit.jsonl"
    audit_log.write_text("", encoding="utf-8")
    engine_lock = tmp_path / "engines.lock.yaml"
    engine_lock.write_text("a: 1", encoding="utf-8")
    checkpoint_lock = tmp_path / "checkpoints.lock.yaml"
    checkpoint_lock.write_text("b: 2", encoding="utf-8")

    payload = build_doctor_payload(
        settings,
        runtime_health=health,
        queue_stats=queue_stats,
        control_instance_id="10676aa6-86e1-424d-a8dd-77f6ce09fc57",
        audit_log=audit_log,
        engine_lock_path=engine_lock,
        checkpoint_lock_path=checkpoint_lock,
        environment_digests={
            "indextts": {"expected": "a", "actual": "a"},
            "gpt_sovits": {"expected": "b", "actual": "b"},
        },
        uv_lock_digest={"expected": "c", "actual": "c"},
        live_inventory_matches={"indextts": True, "gpt_sovits": True},
        pid_registry_stale=False,
        model_revisions_match=True,
        cuda={"available": True, "name": "fake", "uuid": "GPU-0"},
        source_revisions={
            "indextts": "90ca4d608209584bad3a5bd5becc0b80c146e60f",
            "gpt_sovits": "d523079fc05d9a8028d6085bffe4a2757c32abb6",
        },
    )
    assert payload["schema_version"] == 1
    assert payload["workers"]["indextts"]["digest_mismatch"] is False
    assert payload["uv_lock_mismatch"] is False
    assert payload["env_lock_mismatch"] is False
    assert payload["inventory_mismatch"] is False
    assert payload["pid_registry_stale"] is False
    assert payload["model_revision_mismatch"] is False
    assert payload["cuda"]["available"] is True
    assert json.loads(json.dumps(payload)) == payload  # fully JSON-serializable

    result = validate_doctor_payload(payload)
    assert result.status == "ready", result.codes


def test_build_doctor_payload_digest_mismatch_propagates(tmp_path: Path) -> None:
    """digest/env/uv/inventory mismatches all surface as codes."""
    from types import SimpleNamespace

    from voice_pipeline.core.config import load_settings
    from voice_pipeline.runtime.doctor import build_doctor_payload

    health = SimpleNamespace(
        workers=SimpleNamespace(
            indextts=SimpleNamespace(
                state="ready",
                pid=1,
                create_time=1.0,
                python_executable="D:/a/py.exe",
                python_version="3.11.15",
                source_revision="x",
                active_inference=0,
            ),
            gpt_sovits=SimpleNamespace(
                state="ready",
                pid=2,
                create_time=2.0,
                python_executable="D:/b/py.exe",
                python_version="3.11.15",
                source_revision="y",
                active_inference=0,
            ),
        )
    )
    queue_stats = SimpleNamespace(
        state="accepting",
        active_count=0,
        queued_count=0,
        max_active_observed=1,
        max_concurrency=1,
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "app.yaml"
    config.write_text(
        """
schema_version: 1
mode: external_test
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 5.0}
engines:
  indextts:
    base_url: http://127.0.0.1:9
    python_executable: D:/a/py.exe
    repo_dir: repo-index
    request_timeout_seconds: 4.0
    expected_fingerprint: {challenge: external-test-index}
  gpt_sovits:
    base_url: http://127.0.0.1:9
    python_executable: D:/b/py.exe
    repo_dir: repo-gsv
    request_timeout_seconds: 4.0
    expected_fingerprint: {challenge: external-test-gsv}
""",
        encoding="utf-8",
    )
    settings = load_settings(config)
    audit_log = tmp_path / "engine-audit.jsonl"
    audit_log.write_text("", encoding="utf-8")
    engine_lock = tmp_path / "engines.lock.yaml"
    engine_lock.write_text("a: 1", encoding="utf-8")
    checkpoint_lock = tmp_path / "checkpoints.lock.yaml"
    checkpoint_lock.write_text("b: 2", encoding="utf-8")

    payload = build_doctor_payload(
        settings,
        runtime_health=health,
        queue_stats=queue_stats,
        control_instance_id="id",
        audit_log=audit_log,
        engine_lock_path=engine_lock,
        checkpoint_lock_path=checkpoint_lock,
        environment_digests={
            "indextts": {"expected": "a", "actual": "DIFFERENT"},
            "gpt_sovits": None,
        },
        uv_lock_digest={"expected": "c", "actual": "DIFFERENT"},
        live_inventory_matches={"indextts": False, "gpt_sovits": True},
        pid_registry_stale=True,
        model_revisions_match=False,
        cuda=None,
    )
    result = validate_doctor_payload(payload)
    assert result.status == "failed"
    for code in (
        "CHECKPOINT_DIGEST_MISMATCH",
        "UV_LOCK_MISMATCH",
        "ENV_LOCK_MISMATCH",
        "INVENTORY_MISMATCH",
        "PID_REGISTRY_STALE",
        "MODEL_REVISION_MISMATCH",
    ):
        assert code in result.codes, code
    assert payload["cuda"] == {"available": False, "name": None, "uuid": None}
    assert payload["source_revisions"] == {}
