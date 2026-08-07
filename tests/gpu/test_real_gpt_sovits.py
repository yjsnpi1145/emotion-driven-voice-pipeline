"""Real GPT-SoVITS golden gates: target synthesis in two languages."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file


async def _run_segment(
    gpu_settings: Any,
    request: dict[str, Any],
    case_dir: Path,
    *,
    base_url: str,
) -> dict[str, Any]:
    await asyncio.to_thread(case_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        (case_dir / "request.json").write_text,
        json.dumps(request, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    async with httpx.AsyncClient(base_url=base_url, timeout=600) as client:
        resp = await client.post("/api/v1/jobs/segment", json=request)
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

        reference = await client.get(f"/api/v1/jobs/{job_id}/audio/reference")
        assert reference.status_code == 200, reference.text
        await asyncio.to_thread((case_dir / "reference.wav").write_bytes, reference.content)
        target = await client.get(f"/api/v1/jobs/{job_id}/audio/target")
        assert target.status_code == 200, target.text
        await asyncio.to_thread((case_dir / "target.wav").write_bytes, target.content)
        manifest = await client.get(f"/api/v1/jobs/{job_id}/manifest/run")
        assert manifest.status_code == 200, manifest.text
        await asyncio.to_thread((case_dir / "run-manifest.json").write_bytes, manifest.content)
    return {
        "request": request,
        "job_id": job_id,
        "case_dir": case_dir,
        "status": status,
    }


@pytest.mark.gpu
async def test_real_gsv_ja_and_en_targets_are_valid_and_distinct(
    gpu_settings, zh_ja_request, zh_en_request, run_manifest_dir
) -> None:
    base_url = f"http://{gpu_settings.server.host}:{gpu_settings.server.port}"
    ja = await _run_segment(
        gpu_settings, zh_ja_request, run_manifest_dir / "zh-ja-001", base_url=base_url
    )
    en = await _run_segment(
        gpu_settings, zh_en_request, run_manifest_dir / "zh-en-001", base_url=base_url
    )

    for entry, lang in ((ja, "ja"), (en, "en")):
        target = entry["case_dir"] / "target.wav"
        audio = probe_wav(target, require_reference_window=False)
        assert audio.duration_seconds > 0.5, f"{lang} target too short"
        assert audio.rms_dbfs > -50.0, f"{lang} target is silent"
        assert audio.channels == 1
        assert audio.sample_rate == 32000

    ja_sha = sha256_file(ja["case_dir"] / "target.wav")
    en_sha = sha256_file(en["case_dir"] / "target.wav")
    assert ja_sha != en_sha, "ja and en targets must differ"


@pytest.mark.gpu
async def test_manifest_reference_matches_gsv_audit_log(
    gpu_settings, zh_ja_request, run_manifest_dir
) -> None:
    base_url = f"http://{gpu_settings.server.host}:{gpu_settings.server.port}"
    result = await _run_segment(
        gpu_settings, zh_ja_request, run_manifest_dir / "zh-ja-001-audit", base_url=base_url
    )
    manifest = json.loads((result["case_dir"] / "run-manifest.json").read_text(encoding="utf-8"))
    reference_sha = manifest["reference"]["audio"]["content_sha256"]

    # The GSV adapter audit log must record the same reference SHA-256.
    audit_dir = gpu_settings.runtime_dir / "logs"
    matches = []
    for log in audit_dir.rglob("engine-audit.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                row.get("engine") == "gpt_sovits"
                and row.get("reference_sha256_or_null") == reference_sha
            ):
                matches.append(row)
    assert matches, "no gsv audit row recorded the manifest reference SHA-256"
