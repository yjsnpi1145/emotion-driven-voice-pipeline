"""Real cross-language dynamic-challenge gate and doctor/log sanity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from voice_pipeline.modules.audio.wav_probe import sha256_file


@pytest.mark.gpu
async def test_doctor_reports_real_mode(gpu_settings) -> None:
    base_url = f"http://{gpu_settings.server.host}:{gpu_settings.server.port}"
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        health = await client.get("/api/v1/health")
        assert health.status_code == 200, health.text
        payload = health.json()
        assert payload["mode"] == "real"
        assert payload["status"] == "ready"
        assert payload["gpu_queue"]["max_concurrency"] == 1


@pytest.mark.gpu
async def test_dynamic_challenge_goes_through_real_chain(
    gpu_settings, dynamic_challenge, run_manifest_dir, zh_ja_request, zh_en_request
) -> None:
    base_url = f"http://{gpu_settings.server.host}:{gpu_settings.server.port}"
    case_dir = run_manifest_dir / "dynamic"
    case_dir.mkdir(parents=True, exist_ok=True)
    text_sha = hashlib.sha256(dynamic_challenge["target_text"].encode("utf-8")).hexdigest()

    async with httpx.AsyncClient(base_url=base_url, timeout=600) as client:
        resp = await client.post("/api/v1/jobs/segment", json=dynamic_challenge)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        deadline = time.monotonic() + 900
        status = None
        while time.monotonic() < deadline:
            status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
            if status["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(1.0)
        assert status is not None and status["status"] == "succeeded", status

        target = await client.get(f"/api/v1/jobs/{job_id}/audio/target")
        assert target.status_code == 200, target.text
        (case_dir / "target.wav").write_bytes(target.content)
        manifest_resp = await client.get(f"/api/v1/jobs/{job_id}/manifest/run")
        manifest = manifest_resp.json()
        (case_dir / "run-manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        reference = await client.get(f"/api/v1/jobs/{job_id}/audio/reference")
        (case_dir / "reference.wav").write_bytes(reference.content)

    # Dynamic output must differ from both fixed golden outputs.
    dynamic_sha = sha256_file(case_dir / "target.wav")
    for golden in ("zh-ja-001", "zh-en-001"):
        golden_target = run_manifest_dir / golden / "target.wav"
        if golden_target.is_file():
            assert dynamic_sha != sha256_file(golden_target), (
                f"dynamic output identical to fixed golden {golden}"
            )

    # Audit log must contain the dynamic text SHA-256 (never plaintext), the
    # real GSV PID and the model fingerprint.
    audit_dir = gpu_settings.runtime_dir / "logs"
    seen_text_sha = False
    seen_fingerprint = False
    seen_pid = False
    for log in audit_dir.rglob("engine-audit.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("engine") != "gpt_sovits":
                continue
            if row.get("target_text_sha256_or_null") == text_sha:
                seen_text_sha = True
            if row.get("engine_pid") and row.get("engine_pid") > 0:
                seen_pid = True
            fp = row.get("engine_fingerprint")
            if fp and fp.get("model_revision"):
                seen_fingerprint = True
    assert seen_text_sha, "audit log missing dynamic text SHA-256"
    assert seen_pid, "audit log missing real GSV PID"
    assert seen_fingerprint, "audit log missing model fingerprint"


@pytest.mark.gpu
def test_logs_have_no_oom_traceback_or_fake_fallback(gpu_settings, run_manifest_dir) -> None:
    audit_dir = gpu_settings.runtime_dir / "logs"
    forbidden = ("OutOfMemoryError", "CUDA out of memory", "Traceback", "fake")
    matches: list[str] = []
    for log in audit_dir.rglob("*.jsonl"):
        text = log.read_text(encoding="utf-8", errors="replace")
        for token in forbidden:
            if token in text:
                matches.append(f"{log.name}:{token}")
    assert not matches, f"forbidden log markers found: {matches}"
