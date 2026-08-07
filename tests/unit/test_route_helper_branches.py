from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from voice_pipeline.api import routes


def record(kind: str, status: str = "succeeded", result=None):
    return SimpleNamespace(job_id=uuid4(), kind=kind, status=status, result=result or {})


def test_status_and_url_helpers_cover_lifecycle_variants() -> None:
    workers = SimpleNamespace(
        indextts=SimpleNamespace(state="ready"),
        gpt_sovits=SimpleNamespace(state="stopped_expected"),
    )
    runtime = SimpleNamespace(workers=workers)
    assert (
        routes._overall_status(runtime, "exclusive_process", SimpleNamespace(state="accepting"))
        == "ready"
    )
    workers.indextts.state = "stopped_expected"
    workers.gpt_sovits.state = "stopped_expected"
    assert (
        routes._overall_status(runtime, "exclusive_process", SimpleNamespace(state="accepting"))
        == "ready"
    )
    workers.indextts.state = "ready"
    workers.gpt_sovits.state = "starting"
    assert (
        routes._overall_status(runtime, "exclusive_process", SimpleNamespace(state="accepting"))
        == "degraded"
    )
    workers.gpt_sovits.state = "ready"
    assert (
        routes._overall_status(runtime, "resident", SimpleNamespace(state="poisoned")) == "degraded"
    )
    workers.indextts.state = "unknown"
    assert (
        routes._overall_status(runtime, "resident", SimpleNamespace(state="accepting"))
        == "degraded"
    )
    assert routes._urls_for(record("reference"))[0]
    assert routes._urls_for(record("gsv"))[1]
    assert len(routes._urls_for(record("segment"))[0]) == 2
    assert routes._urls_for(record("unknown")) == ([], [])
    assert routes._urls_for(record("segment", status="running")) == ([], [])


def test_path_helpers_and_terminal_errors(tmp_path) -> None:
    target = tmp_path / "target.wav"
    target.write_bytes(b"x")
    manifest = tmp_path / "run.json"
    manifest.write_text("{}", encoding="utf-8")
    assert (
        routes._audio_path(record("gsv", result={"target": {"path": str(target)}}), "target")
        == target
    )
    assert routes._audio_path(record("unknown"), "target") is None
    assert (
        routes._manifest_path(record("gsv", result={"manifest_path": str(manifest)}), "run")
        == manifest
    )
    assert routes._manifest_path(record("unknown"), "run") is None

    plane = SimpleNamespace(registry=SimpleNamespace())

    async def missing(_job):
        raise KeyError

    plane.registry.get = missing
    with pytest.raises(HTTPException, match="job not found"):
        import asyncio

        asyncio.run(routes._require_job(plane, uuid4()))
