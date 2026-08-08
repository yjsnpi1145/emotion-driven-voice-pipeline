from __future__ import annotations

from pathlib import Path

from voice_pipeline.api import dependencies
from voice_pipeline.core.config import AppSettings
from voice_pipeline.models.schemas import EngineFingerprint


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings.model_validate(
        {
            "schema_version": 1,
            "mode": "real",
            "engine_lifecycle": "exclusive_process",
            "server": {"host": "127.0.0.1", "port": 8765},
            "runtime_dir": str(tmp_path / "runtime"),
            "engine_lock_path": str(tmp_path / "engines.lock.yaml"),
            "checkpoint_lock_path": str(tmp_path / "checkpoints.lock.yaml"),
            "queue": {"max_concurrency": 1, "queue_timeout_seconds": 60},
            "engines": {
                "indextts": {
                    "base_url": "http://127.0.0.1:9871",
                    "python_executable": str(tmp_path / "index-python.exe"),
                    "repo_dir": str(tmp_path / "index"),
                    "request_timeout_seconds": 300,
                },
                "gpt_sovits": {
                    "base_url": "http://127.0.0.1:9880",
                    "python_executable": str(tmp_path / "gsv-python.exe"),
                    "repo_dir": str(tmp_path / "gsv"),
                    "request_timeout_seconds": 300,
                },
            },
        }
    )


def _fingerprint(engine: str) -> EngineFingerprint:
    return EngineFingerprint(
        schema_version=1,
        engine=engine,  # type: ignore[arg-type]
        source_revision="source",
        model_revision="model",
        engine_lock_sha256="0" * 64,
        checkpoint_lock_sha256="1" * 64,
        environment_lock_sha256="2" * 64,
        runtime_config_sha256="3" * 64,
    )


def test_real_dependencies_wire_process_manager_and_supervisor(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}

    class Manager:
        def __init__(self, **kwargs: object) -> None:
            calls["manager"] = kwargs

    class Supervisor:
        def __init__(
            self, *, mode: str, processes: object, fingerprints: object, **kwargs: object
        ) -> None:
            calls["supervisor"] = {
                "mode": mode,
                "processes": processes,
                "fingerprints": fingerprints,
                **kwargs,
            }

        def fingerprint(self, engine: str) -> EngineFingerprint:
            return _fingerprint(engine)

    class Client:
        def __init__(self, **kwargs: object) -> None:
            calls.setdefault("clients", []).append(kwargs)  # type: ignore[union-attr]

    classes = {
        ("voice_pipeline.runtime.process", "RealWorkerProcessManager"): Manager,
        ("voice_pipeline.runtime.supervisor", "ProcessSupervisor"): Supervisor,
        ("voice_pipeline.modules.indextts.client", "IndexTTSHttpClient"): Client,
        ("voice_pipeline.modules.gpt_sovits.client", "GptSoVitsHttpClient"): Client,
    }
    monkeypatch.setattr(
        dependencies, "_require_module", lambda module, attr: classes[(module, attr)]
    )
    monkeypatch.setattr(
        dependencies, "compute_engine_fingerprint", lambda engine, **_: _fingerprint(engine)
    )

    dependencies._build_real(_settings(tmp_path))

    assert calls["supervisor"]["mode"] == "exclusive_process"  # type: ignore[index]
    assert calls["manager"]["jobs_root"] == tmp_path / "runtime" / "jobs"  # type: ignore[index]
    assert len(calls["clients"]) == 2  # type: ignore[arg-type]
