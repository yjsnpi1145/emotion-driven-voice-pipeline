from __future__ import annotations

import asyncio

from voice_pipeline.api.app import ControlPlane, create_app
from voice_pipeline.core.config import load_settings


def test_create_app_uses_all_explicit_dependency_overrides(tmp_path) -> None:
    config = tmp_path / "app.yaml"
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
    index, gsv, runtime = object(), object(), object()
    app = create_app(
        load_settings(config), index_client=index, gsv_client=gsv, engine_runtime=runtime
    )
    assert app.state.plane.index is index
    assert app.state.plane.gsv is gsv
    assert app.state.plane.runtime is runtime


def test_abort_all_active_skips_stopped_worker_and_ignores_abort_error() -> None:
    called: list[str] = []

    class Runtime:
        def health(self):
            workers = type(
                "Workers",
                (),
                {
                    "indextts": type("Worker", (), {"state": "stopped_expected"})(),
                    "gpt_sovits": type("Worker", (), {"state": "ready"})(),
                },
            )()
            return type("Health", (), {"workers": workers})()

        async def abort_engine(self, engine, **_kwargs):
            called.append(engine)
            raise RuntimeError("expected test failure")

    plane = object.__new__(ControlPlane)
    plane.runtime = Runtime()
    asyncio.run(plane._abort_all_active(1.0))
    assert called == ["gpt_sovits"]


def test_shutdown_tolerates_runtime_timeout_and_calls_exit_callback() -> None:
    calls: list[str] = []

    class Queue:
        async def stop(self, **_kwargs):
            calls.append("queue")

    class Runtime:
        async def stop(self, **_kwargs):
            raise TimeoutError

    plane = object.__new__(ControlPlane)
    plane.queue = Queue()
    plane.runtime = Runtime()
    plane._shutdown_started = False
    plane._accepting = True
    plane._exit_callback = lambda: calls.append("exit")
    asyncio.run(plane.shutdown())
    assert calls == ["queue", "exit"]
    assert plane.accepting is False
