from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _fingerprint_json() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "engine": "indextts",
            "source_revision": "90ca4d608209584bad3a5bd5becc0b80c146e60f",
            "model_revision": "740dcaff396282ffb241903d150ac011cd4b1ede",
            "engine_lock_sha256": "0" * 64,
            "checkpoint_lock_sha256": "1" * 64,
            "environment_lock_sha256": "2" * 64,
            "runtime_config_sha256": "3" * 64,
        }
    )


def test_worker_main_help_exits_zero() -> None:
    run = subprocess.run(
        [sys.executable, "-m", "workers.indextts2", "--help"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0
    assert "--repo-dir" in run.stdout
    assert "--expected-fingerprint-json" in run.stdout


def test_worker_main_missing_repo_dir_exits_two(tmp_path: Path) -> None:
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "workers.indextts2",
            "--port",
            "19999",
            "--repo-dir",
            str(tmp_path / "does-not-exist"),
            "--model-dir",
            str(tmp_path),
            "--aux-root",
            str(tmp_path),
            "--jobs-root",
            str(tmp_path),
            "--engine-lock",
            str(tmp_path / "e.yaml"),
            "--checkpoint-lock",
            str(tmp_path / "c.yaml"),
            "--environment-lock",
            str(tmp_path / "ev.yaml"),
            "--environment-freeze",
            str(tmp_path / "ef.txt"),
            "--expected-fingerprint-json",
            _fingerprint_json(),
        ],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 2
    assert "repo dir not found" in run.stderr


def test_worker_main_full_startup_with_monkeypatched_deps(tmp_path: Path, monkeypatch) -> None:
    """main() wiring: fingerprint parse, RealIndexEngine construction,
    worker app creation and uvicorn launch all run with fake deps."""
    import workers.indextts2.__main__ as entry
    import workers.indextts2.app as app_mod
    import workers.indextts2.engine as engine_mod

    captured: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, model_dir, aux_paths, device="cuda:0"):
            captured["model_dir"] = str(model_dir)
            captured["aux_paths"] = aux_paths
            captured["device"] = device

    class FakeWorkerApp:
        def __init__(self, engine, *, jobs_root, expected_fingerprint):
            captured["engine"] = engine
            captured["jobs_root"] = str(jobs_root)
            captured["fingerprint_engine"] = expected_fingerprint.engine

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, workers, log_level):
            captured["uvicorn_run"] = (port, workers, log_level)

    monkeypatch.setattr(engine_mod, "RealIndexEngine", FakeEngine)
    monkeypatch.setattr(app_mod, "create_worker_app", FakeWorkerApp)
    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    aux_root = tmp_path / "aux"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "workers.indextts2",
            "--port",
            "19998",
            "--repo-dir",
            str(tmp_path),
            "--model-dir",
            str(model_dir),
            "--aux-root",
            str(aux_root),
            "--jobs-root",
            str(tmp_path),
            "--engine-lock",
            str(tmp_path / "e.yaml"),
            "--checkpoint-lock",
            str(tmp_path / "c.yaml"),
            "--environment-lock",
            str(tmp_path / "ev.yaml"),
            "--environment-freeze",
            str(tmp_path / "ef.txt"),
            "--expected-fingerprint-json",
            _fingerprint_json(),
            "--device",
            "cuda:7",
        ],
    )
    entry.main()
    assert captured["model_dir"] == str(model_dir.resolve())
    assert captured["aux_paths"] == {
        "w2v_bert": str(aux_root / "w2v_bert"),
        "semantic_codec": str(aux_root / "maskgct" / "semantic_codec" / "model.safetensors"),
        "campplus": str(aux_root / "campplus" / "campplus_cn_common.bin"),
        "bigvgan": str(aux_root / "bigvgan"),
    }
    assert captured["device"] == "cuda:7"
    assert captured["uvicorn_run"] == (19998, 1, "info")


def test_control_main_module_help_exits_zero() -> None:
    run = subprocess.run(
        [sys.executable, "-m", "voice_pipeline", "--help"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0
    assert "serve" in run.stdout


def test_real_index_engine_synthesizes_with_fake_upstream(tmp_path: Path, monkeypatch) -> None:
    from workers.indextts2.engine import RealIndexEngine, _write_simple_wav
    from workers.indextts2.schemas import WorkerSynthesisRequest

    captured: dict[str, object] = {}

    class FakeTorch:
        def manual_seed(self, value: int) -> None:
            captured["torch_seed"] = value

        class cuda:
            @staticmethod
            def manual_seed_all(value: int) -> None:
                captured["cuda_seed"] = value

    class FakeIndexTTS2:
        def __init__(self, **kwargs: object) -> None:
            captured["init_kwargs"] = kwargs

        def infer(self, **kwargs: object) -> None:
            captured["infer_kwargs"] = kwargs
            _write_simple_wav(Path(kwargs["output_path"]), seconds=1.0)

    fake_infer_v2 = types.ModuleType("indextts.infer_v2")
    fake_infer_v2.IndexTTS2 = FakeIndexTTS2
    fake_pkg = types.ModuleType("indextts")
    fake_pkg.infer_v2 = fake_infer_v2
    monkeypatch.setitem(sys.modules, "indextts", fake_pkg)
    monkeypatch.setitem(sys.modules, "indextts.infer_v2", fake_infer_v2)
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("model: fake\n", encoding="utf-8")
    aux_paths = {
        k: str(tmp_path / k) for k in ("w2v_bert", "semantic_codec", "campplus", "bigvgan")
    }
    engine = RealIndexEngine(model_dir, aux_paths, device="cuda:0")
    assert captured["init_kwargs"]["device"] == "cuda:0"
    assert captured["init_kwargs"]["use_fp16"] is True

    out = tmp_path / "out.wav"
    request = WorkerSynthesisRequest(
        request_id=uuid4(),
        text="私はまだ生きている。",
        speaker_audio_path=tmp_path / "spk.wav",
        emotion_vector=(0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20),
        seed=42,
        output_path=out,
    )
    engine.synthesize(request)
    assert out.is_file()
    assert out.stat().st_size > 0
    assert captured["torch_seed"] == 42
    assert captured["cuda_seed"] == 42
    infer_kwargs = captured["infer_kwargs"]
    assert infer_kwargs["emo_vector"] == list(request.emotion_vector)
    assert infer_kwargs["emo_alpha"] == 1.0


def test_real_index_engine_rejects_partial_aux(tmp_path: Path) -> None:
    from workers.indextts2.engine import RealIndexEngine

    with pytest.raises(ValueError, match="all four pinned auxiliary"):
        RealIndexEngine(
            tmp_path,
            {"w2v_bert": "x", "semantic_codec": "y", "campplus": "z"},
            device="cuda:0",
        )
