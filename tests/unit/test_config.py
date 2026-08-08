from pathlib import Path

import pytest

from voice_pipeline.core.config import ModelLibrarySettings, load_settings


def test_load_settings_resolves_paths_from_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "配置 目录"
    config_dir.mkdir()
    config = config_dir / "app.local.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server:
  host: 127.0.0.1
  port: 8765
runtime_dir: ../运行 输出
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue:
  max_concurrency: 1
  queue_timeout_seconds: 60
engines:
  indextts:
    base_url: http://127.0.0.1:9871
    python_executable: ../index/.venv/Scripts/python.exe
    repo_dir: ../index
    request_timeout_seconds: 300
  gpt_sovits:
    base_url: http://127.0.0.1:9880
    python_executable: ../gsv/.venv/Scripts/python.exe
    repo_dir: ../gsv
    request_timeout_seconds: 300
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.runtime_dir == (config_dir / "../运行 输出").resolve()
    assert settings.queue.max_concurrency == 1
    assert settings.server.host == "127.0.0.1"


def test_rejects_more_than_one_gpu_consumer(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 8765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 2, queue_timeout_seconds: 60}
engines:
  indextts:
    base_url: http://127.0.0.1:9871
    python_executable: i.exe
    repo_dir: i
    request_timeout_seconds: 300
  gpt_sovits:
    base_url: http://127.0.0.1:9880
    python_executable: g.exe
    repo_dir: g
    request_timeout_seconds: 300
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="max_concurrency must be exactly 1"):
        load_settings(config)


def test_accepts_external_http_test_mode(tmp_path: Path) -> None:
    config = tmp_path / "external-test.yaml"
    config.write_text(
        """
schema_version: 1
mode: external_test
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 2}
engines:
  indextts:
    {base_url: http://127.0.0.1:19001, python_executable: index-python.exe, repo_dir: index,
     request_timeout_seconds: 2, expected_fingerprint: {challenge: index-123}}
  gpt_sovits:
    {base_url: http://127.0.0.1:19002, python_executable: gsv-python.exe, repo_dir: gsv,
     request_timeout_seconds: 2, expected_fingerprint: {challenge: gsv-456}}
""",
        encoding="utf-8",
    )

    assert load_settings(config).mode == "external_test"


def test_model_library_rejects_a_root_that_overlaps_runtime(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="models_root"):
        ModelLibrarySettings(
            models_root=tmp_path / "runtime",
            allowed_import_roots=[tmp_path / "imports"],
        ).validate_against_runtime(tmp_path / "runtime")


def test_real_mode_requires_locked_faster_whisper_quality_config(tmp_path: Path) -> None:
    config = tmp_path / "real.yaml"
    config.write_text(
        """
schema_version: 1
mode: real
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 8765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 60}
quality: {mode: fake}
engines:
  indextts:
    {base_url: http://127.0.0.1:9871, python_executable: i.exe, repo_dir: i,
     request_timeout_seconds: 300}
  gpt_sovits:
    {base_url: http://127.0.0.1:9880, python_executable: g.exe, repo_dir: g,
     request_timeout_seconds: 300}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="faster_whisper"):
        load_settings(config)
