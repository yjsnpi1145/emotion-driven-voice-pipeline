"""GPU residency lifecycle-decision validator.

Marked ``gpu_residency``; invoked only by
``scripts/probe-engine-lifecycle.ps1`` against the evidence directory. The
formal GPU suite uses ``-m "gpu and not gpu_residency"`` so it never re-probes
or re-triggers OOM inside an already-started final configuration.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _evidence_dir() -> Path:
    evidence = os.environ.get("VOICE_PIPELINE_GPU_EVIDENCE")
    if not evidence:
        pytest.skip("VOICE_PIPELINE_GPU_EVIDENCE not set")
    path = Path(evidence).resolve()
    if not path.is_dir():
        pytest.fail(f"evidence dir missing: {path}")
    return path


@pytest.mark.gpu_residency
def test_lifecycle_decision_schema_and_memory_budget() -> None:
    evidence = _evidence_dir()
    decision_path = evidence / "lifecycle-decision.json"
    if not decision_path.is_file():
        pytest.fail(f"lifecycle-decision.json missing: {decision_path}")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))

    assert decision["schema_version"] == 1
    assert decision["status"] in (
        "resident_supported",
        "exclusive_required",
        "probe_failed",
    )
    assert decision["effective_lifecycle"] in ("resident", "exclusive_process", None)

    gpu = decision["gpu"]
    assert isinstance(gpu["uuid"], str) and gpu["uuid"]
    assert isinstance(gpu["name"], str) and gpu["name"]
    assert isinstance(gpu["total_mib"], int) and gpu["total_mib"] > 0

    memory = decision["memory_mib"]
    for field in (
        "idle",
        "index_peak",
        "gsv_peak",
        "combined_peak",
        "required_reserve",
        "margin",
    ):
        assert isinstance(memory[field], int), field
        assert memory[field] >= 0, field

    classification = decision["classification"]
    assert classification["kind"] in (
        "none",
        "cuda_oom",
        "insufficient_margin",
        "probe_error",
    )
    assert isinstance(classification["oom_detected"], bool)
    assert isinstance(classification["rule"], str)
    assert isinstance(classification["source_log_sha256"], list)

    # Budget arithmetic is consistent.
    assert memory["margin"] == (
        gpu["total_mib"] - memory["combined_peak"] - memory["required_reserve"]
    )
    # Numeric evidence is required to fall back to exclusive.
    if decision["status"] == "exclusive_required":
        assert (
            classification["kind"] == "cuda_oom"
            or memory["combined_peak"] + memory["required_reserve"] > gpu["total_mib"]
        )
    if decision["status"] == "resident_supported":
        assert classification["kind"] == "none"
        assert decision["effective_lifecycle"] == "resident"

    # Every candidate PID/create-time must appear in a verified stop receipt.
    stop_receipt = evidence / "stop-receipt.json"
    if stop_receipt.is_file():
        receipt = json.loads(stop_receipt.read_text(encoding="utf-8"))
        stopped = {(int(p["pid"]), float(p["create_time"])) for p in receipt.get("processes", [])}
        for candidate in decision["candidate_processes"]:
            assert (int(candidate["pid"]), float(candidate["create_time"])) in stopped, (
                f"candidate {candidate} not verified stopped"
            )
            assert candidate["verified_exited"] is True
    else:
        assert decision["candidate_processes"] == []

    assert (
        decision["stop_receipt_sha256"]
        == re.fullmatch(r"[0-9a-f]{64}", decision["stop_receipt_sha256"]).group()
        if decision["stop_receipt_sha256"]
        else True
    )
    assert isinstance(decision["evidence_paths"], list)


@pytest.mark.gpu_residency
def test_effective_config_preserves_resident_lifecycle() -> None:
    evidence = _evidence_dir()
    decision_path = evidence / "lifecycle-decision.json"
    if not decision_path.is_file():
        pytest.fail(f"lifecycle-decision.json missing: {decision_path}")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    effective = evidence / "effective.gpu.yaml"
    if decision["status"] == "resident_supported":
        if not effective.is_file():
            pytest.fail("effective.gpu.yaml missing under resident_supported")
        text = effective.read_text(encoding="utf-8")
        assert "engine_lifecycle: resident" in text
    elif decision["status"] == "exclusive_required":
        if not effective.is_file():
            pytest.fail("effective.gpu.yaml missing under exclusive_required")
        text = effective.read_text(encoding="utf-8")
        assert "engine_lifecycle: exclusive_process" in text
