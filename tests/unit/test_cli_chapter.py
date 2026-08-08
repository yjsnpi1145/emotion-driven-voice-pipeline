from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from tests.integration_cpu.conftest import write_tone
from voice_pipeline.cli import app

runner = CliRunner()


def test_synthesize_chapter_cli_downloads_final_and_timeline_over_http(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.wav"
    write_tone(source, 5.0)
    wav = source.read_bytes()
    run_id = str(uuid4())

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, path: str, **_kwargs):
            assert path == "/api/v1/chapters"
            return httpx.Response(
                202, json={"run_id": run_id}, request=httpx.Request("POST", "http://x")
            )

        def get(self, path: str):
            if path == f"/api/v1/chapters/{run_id}":
                return httpx.Response(
                    200, json={"status": "succeeded"}, request=httpx.Request("GET", "http://x")
                )
            if path == f"/api/v1/chapters/{run_id}/timeline":
                return httpx.Response(
                    200,
                    json={"schema_version": 1, "segments": [], "duration_seconds": 5.0},
                    request=httpx.Request("GET", "http://x"),
                )
            raise AssertionError(path)

        @contextmanager
        def stream(self, _method: str, path: str):
            assert path == f"/api/v1/chapters/{run_id}/audio"
            yield httpx.Response(200, content=wav, request=httpx.Request("GET", "http://x"))

    monkeypatch.setattr("voice_pipeline.cli._client", lambda *_args: FakeClient())
    request = tmp_path / "chapter.json"
    request.write_text(
        json.dumps(
            {
                "request_id": str(uuid4()),
                "title": "chapter",
                "source_text": "正文。",
                "target_language": "ja",
                "base_voice_path": str(source),
                "model_profile_id": str(uuid4()),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    result = runner.invoke(
        app,
        [
            "synthesize-chapter",
            "--server",
            "http://control",
            "--request",
            str(request),
            "--output-dir",
            str(output_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (output_dir / "final.wav").is_file()
    assert (output_dir / "timeline.json").is_file()
    assert json.loads(result.stdout)["status"] == "succeeded"
