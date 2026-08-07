from __future__ import annotations

import importlib
import json
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
import typer
from typer.testing import CliRunner

from voice_pipeline import cli
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import ReferenceJobRequest


def _response(status: int, *, content: bytes = b"{}") -> httpx.Response:
    return httpx.Response(
        status,
        content=content,
        request=httpx.Request("GET", "http://control.invalid"),
    )


def test_http_error_classification_and_invalid_request_paths(tmp_path: Path) -> None:
    with pytest.raises(PipelineError) as unavailable:
        cli._handle_http_error(_response(503), "/health")
    assert unavailable.value.code == ErrorCode.CONTROL_PLANE_UNAVAILABLE

    with pytest.raises(PipelineError) as unmapped:
        cli._handle_http_error(_response(500, content=b"not-json"), "/health")
    assert unmapped.value.code == ErrorCode.ENGINE_UNAVAILABLE

    missing = tmp_path / "missing.json"
    with pytest.raises(typer.Exit):
        cli._load_request(ReferenceJobRequest, missing)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    with pytest.raises(typer.Exit):
        cli._load_request(ReferenceJobRequest, invalid)


def test_doctor_non_json_and_error_output(monkeypatch) -> None:
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _path: str):
            return _response(
                200, content=b'{"status":"ready","mode":"fake","engine_lifecycle":"resident"}'
            )

    monkeypatch.setattr(cli, "_client", lambda *_args: Client())
    cli.doctor("http://control", False)

    class Failure(Client):
        def get(self, _path: str):
            return _response(
                500, content=b'{"error":{"code":"ENGINE_UNAVAILABLE","message":"bad"}}'
            )

    monkeypatch.setattr(cli, "_client", lambda *_args: Failure())
    with pytest.raises(typer.Exit):
        cli.doctor("http://control", True)


def test_cli_app_help_and_entry_main_are_executable() -> None:
    assert CliRunner().invoke(cli.app, ["--help"]).exit_code == 0
    import voice_pipeline.__main__ as entry

    assert entry is not None


def test_cli_private_error_helpers(monkeypatch, tmp_path: Path) -> None:
    for code, expected in (
        (ErrorCode.INVALID_INPUT, 2),
        (ErrorCode.ENGINE_UNAVAILABLE, 3),
        (ErrorCode.QUEUE_TIMEOUT, 5),
        (ErrorCode.INVALID_AUDIO, 4),
    ):
        assert cli._exit_code_for(PipelineError(code, "test", "x", retryable=False)) == expected

    with pytest.raises(PipelineError, match="job ended"):
        cli._fail_from_status({"status": "failed"})

    class BadClient:
        def get(self, _path: str):
            return _response(500)

    with pytest.raises(PipelineError):
        cli._fetch_manifest(BadClient(), "job", "run")

    class PendingClient:
        def get(self, _path: str):
            return _response(200, content=b'{"status":"running"}')

    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    with pytest.raises(PipelineError, match="did not finish"):
        cli._poll_job(PendingClient(), "job", 1)

    reservation = cli.reserve_output_path(tmp_path / "target.wav")

    class BadStream:
        @contextmanager
        def stream(self, *_args):
            yield _response(500)

    with pytest.raises(PipelineError):
        cli._download_to_reservation(BadStream(), "/audio", reservation, probe_reference=False)
    assert not (tmp_path / "target.wav").exists()


def test_each_synthesis_command_maps_submit_and_failed_job_errors(
    monkeypatch, tmp_path: Path
) -> None:
    class FailureClient:
        def __init__(self, submit_status: int, job_status: str = "failed") -> None:
            self.submit_status = submit_status
            self.job_status = job_status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _path: str, **_kwargs):
            payload = {"job_id": "cc5a5d03-d18a-4c40-9d27-a6f6904702a4"}
            return _response(self.submit_status, content=json_bytes(payload))

        def get(self, _path: str):
            return _response(200, content=json_bytes({"status": self.job_status}))

    source = tmp_path / "source.wav"
    source.write_bytes(b"x")
    common = {
        "request_id": "cc5a5d03-d18a-4c40-9d27-a6f6904702a4",
        "base_voice_path": str(source),
        "ref_text_cn": "参考",
        "emotion_vector": [0.1] * 8,
    }
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(common), encoding="utf-8")
    gsv = tmp_path / "gsv.json"
    gsv.write_text(
        json.dumps(
            {
                "request_id": common["request_id"],
                "reference_manifest_path": str(tmp_path / "missing.json"),
                "target_text": "hello",
                "target_language": "en",
            }
        ),
        encoding="utf-8",
    )
    segment = tmp_path / "segment.json"
    segment.write_text(
        json.dumps({**common, "target_text": "hello", "target_language": "en"}),
        encoding="utf-8",
    )
    commands = (
        (cli.generate_reference, ("http://control", ref, tmp_path / "r.wav", True, 1)),
        (cli.generate_gsv, ("http://control", gsv, tmp_path / "g.wav", True, 1)),
        (cli.synthesize_segment, ("http://control", segment, tmp_path / "out", True, 1)),
    )
    for status in (500, 202):
        monkeypatch.setattr(cli, "_client", lambda *_args, s=status: FailureClient(s))
        for command, args in commands:
            with pytest.raises(typer.Exit):
                command(*args)


def json_bytes(payload: dict[str, str]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_serve_constructs_a_single_worker_uvicorn_server(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Plane:
        def set_exit_callback(self, callback):
            captured["callback"] = callback

    app_obj = SimpleNamespace(state=SimpleNamespace(plane=Plane()))
    settings = SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=17865))

    class Config:
        def __init__(self, app, **kwargs):
            kwargs["app"] = app
            captured["config"] = kwargs

    class Server:
        def __init__(self, config):
            self.should_exit = False
            captured["server"] = self
            captured["server_config"] = config

        def run(self):
            captured["ran"] = True

    uvicorn = ModuleType("uvicorn")
    uvicorn.Config = Config  # type: ignore[attr-defined]
    uvicorn.Server = Server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setattr("voice_pipeline.core.config.load_settings", lambda _path: settings)
    monkeypatch.setattr("voice_pipeline.api.app.create_app", lambda _settings: app_obj)
    cli.serve(tmp_path / "app.yaml")
    assert captured["ran"] is True
    assert captured["config"] == {
        "app": app_obj,
        "host": "127.0.0.1",
        "port": 17865,
        "workers": 1,
        "reload": False,
    }


def test_module_entrypoints_execute_their_main_guards(monkeypatch) -> None:
    called: list[str] = []
    fake_cli = ModuleType("voice_pipeline.cli")
    fake_cli.app = lambda: called.append("app")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "voice_pipeline.cli", fake_cli)
    monkeypatch.delitem(sys.modules, "voice_pipeline.main", raising=False)
    monkeypatch.delitem(sys.modules, "voice_pipeline.__main__", raising=False)
    importlib.import_module("voice_pipeline.main")
    importlib.import_module("voice_pipeline.__main__")
    monkeypatch.delitem(sys.modules, "voice_pipeline.main", raising=False)
    monkeypatch.delitem(sys.modules, "voice_pipeline.__main__", raising=False)
    runpy.run_module("voice_pipeline.main", run_name="__main__")
    runpy.run_module("voice_pipeline.__main__", run_name="__main__")
    assert called == ["app", "app"]
