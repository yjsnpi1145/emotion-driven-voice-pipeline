from __future__ import annotations

from pathlib import Path

import pytest

from voice_pipeline.core.config import load_settings


@pytest.fixture
def fake_settings(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "app.fake.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 5}
engines:
  indextts:
    base_url: http://127.0.0.1:19871
    python_executable: i.exe
    repo_dir: i
    request_timeout_seconds: 10
  gpt_sovits:
    base_url: http://127.0.0.1:19880
    python_executable: g.exe
    repo_dir: g
    request_timeout_seconds: 10
""",
        encoding="utf-8",
    )
    return load_settings(config)
