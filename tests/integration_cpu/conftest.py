from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.config import load_settings


def write_tone(
    path: Path,
    seconds: float,
    amplitude: float = 0.2,
    sample_rate: int = 22050,
) -> None:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = amplitude * np.sin(2 * np.pi * 220 * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


@pytest.fixture
def external_servers(tmp_path: Path):
    from tests.fixtures.external_harness import FakeServerProcess

    ready_dir = tmp_path / "fake-servers"
    ready_dir.mkdir()
    index_server = FakeServerProcess("indextts", ready_dir, delay_ms=400)
    gsv_server = FakeServerProcess("gpt_sovits", ready_dir, delay_ms=400)
    index_server.start()
    gsv_server.start()
    try:
        yield index_server, gsv_server
    finally:
        index_server.stop()
        gsv_server.stop()


@pytest.fixture
def external_settings(external_servers, tmp_path: Path):
    from tests.fixtures.external_harness import build_external_config

    index_server, gsv_server = external_servers
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = build_external_config(
        config_dir=config_dir,
        runtime_dir=tmp_path / "runtime",
        index_server=index_server,
        gsv_server=gsv_server,
        queue_timeout_seconds=1.0,
        request_timeout_seconds=4.0,
    )
    return load_settings(config)


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
queue: {max_concurrency: 1, queue_timeout_seconds: 1}
engines:
  indextts:
    base_url: http://127.0.0.1:19871
    python_executable: i.exe
    repo_dir: i
    request_timeout_seconds: 2
  gpt_sovits:
    base_url: http://127.0.0.1:19880
    python_executable: g.exe
    repo_dir: g
    request_timeout_seconds: 2
""",
        encoding="utf-8",
    )
    return load_settings(config)


@pytest.fixture
def request_json(tmp_path: Path) -> dict[str, object]:
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, 5.0)
    return {
        "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
        "base_voice_path": str(base_voice.resolve()),
        "ref_text_cn": "我已经失去了一切，可我仍然活着。",
        "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
        "target_text": "私はまだ生きている。",
        "target_language": "ja",
        "seed": 1234,
    }
