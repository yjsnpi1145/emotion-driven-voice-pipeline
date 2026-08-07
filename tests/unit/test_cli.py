from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import soundfile as sf
from typer.testing import CliRunner

from voice_pipeline.cli import app
from voice_pipeline.models.schemas import SegmentSynthesisRequest

runner = CliRunner()


def write_tone(path: Path, seconds: float) -> None:
    sample_rate = 22050
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = 0.2 * np.sin(2 * np.pi * 220 * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def test_synthesize_never_falls_back_when_server_is_down(tmp_path) -> None:
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, seconds=5.0)
    request = tmp_path / "request.json"
    request.write_text(
        SegmentSynthesisRequest(
            request_id="d955a4a2-bf44-4a49-a82c-2962eb602d75",
            base_voice_path=base_voice.resolve(),
            ref_text_cn="我依然会向前走。",
            emotion_vector=[0.0, 0.0, 0.2, 0.0, 0.0, 0.2, 0.0, 0.2],
            target_text="I will keep moving forward.",
            target_language="en",
            seed=1234,
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://127.0.0.1:1",
            "--request",
            str(request),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONTROL_PLANE_UNAVAILABLE"
    assert not (tmp_path / "out").exists()


def test_invalid_request_returns_2_without_http(tmp_path) -> None:
    request = tmp_path / "request.json"
    request.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://127.0.0.1:1",
            "--request",
            str(request),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "INVALID_INPUT"
    assert not (tmp_path / "out").exists()


def test_all_successful_cli_synthesis_commands(monkeypatch, tmp_path: Path) -> None:
    """Exercise all public CLI download/publish paths with an HTTP-only fake."""
    source = tmp_path / "source.wav"
    write_tone(source, seconds=5.0)
    wav = source.read_bytes()
    digest = hashlib.sha256(wav).hexdigest()
    audio = {
        "path": str(source),
        "duration_seconds": 5.0,
        "sample_rate": 22050,
        "channels": 1,
        "frames": 110250,
        "content_sha256": digest,
        "rms_dbfs": -17.0,
        "peak_dbfs": -14.0,
    }
    fingerprint = {
        "schema_version": 1,
        "engine": "indextts",
        "source_revision": "test",
        "model_revision": "test",
        "engine_lock_sha256": "a" * 64,
        "checkpoint_lock_sha256": "b" * 64,
        "environment_lock_sha256": "c" * 64,
        "runtime_config_sha256": "d" * 64,
    }
    binding = {
        "audio": audio,
        "ref_text_cn": "参考文本",
        "emotion_vector": [0.1] * 8,
        "base_voice_sha256": digest,
        "engine_fingerprint": fingerprint,
    }
    reference_manifest = {"reference": binding}
    run_manifest = {"reference": binding, "request": {"seed": 7}}

    def response(status: int, *, body: dict | None = None) -> httpx.Response:
        return httpx.Response(
            status,
            json=body or {},
            request=httpx.Request("GET", "http://control.invalid"),
        )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, path: str, **_kwargs):
            return response(202, body={"job_id": str(uuid4())})

        def get(self, path: str):
            if "/jobs/" in path and "/manifest/" not in path and "/audio/" not in path:
                return response(200, body={"status": "succeeded"})
            if path.endswith("/manifest/reference"):
                return response(200, body=reference_manifest)
            if path.endswith("/manifest/run"):
                return response(200, body=run_manifest)
            raise AssertionError(path)

        @contextmanager
        def stream(self, _method: str, path: str):
            assert "/audio/" in path
            yield httpx.Response(
                200,
                content=wav,
                headers={"Content-Type": "audio/wav"},
                request=httpx.Request("GET", "http://control.invalid"),
            )

    monkeypatch.setattr("voice_pipeline.cli._client", lambda *_args: FakeClient())
    request_id = "cc5a5d03-d18a-4c40-9d27-a6f6904702a4"
    ref_request = tmp_path / "reference.json"
    ref_request.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "base_voice_path": str(source),
                "ref_text_cn": "参考文本",
                "emotion_vector": [0.1] * 8,
                "seed": 7,
            }
        ),
        encoding="utf-8",
    )
    ref_out = tmp_path / "reference.wav"
    assert (
        runner.invoke(
            app,
            [
                "generate-reference",
                "--server",
                "http://control",
                "--request",
                str(ref_request),
                "--output",
                str(ref_out),
                "--json",
            ],
        ).exit_code
        == 0
    )
    manifest = ref_out.with_name("reference.reference-manifest.json")

    gsv_request = tmp_path / "gsv.json"
    gsv_request.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "reference_manifest_path": str(manifest),
                "target_text": "hello",
                "target_language": "en",
            }
        ),
        encoding="utf-8",
    )
    assert (
        runner.invoke(
            app,
            [
                "generate-gsv",
                "--server",
                "http://control",
                "--request",
                str(gsv_request),
                "--output",
                str(tmp_path / "target.wav"),
                "--json",
            ],
        ).exit_code
        == 0
    )

    segment_request = tmp_path / "segment.json"
    segment_request.write_text(
        SegmentSynthesisRequest(
            request_id=request_id,
            base_voice_path=source,
            ref_text_cn="参考文本",
            emotion_vector=[0.1] * 8,
            target_text="hello",
            target_language="en",
            seed=7,
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://control",
            "--request",
            str(segment_request),
            "--output-dir",
            str(tmp_path / "segment"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Typer's isolated runner executes command callbacks outside coverage's
    # current context on Windows.  Invoke the public callbacks directly too,
    # retaining the same HTTP boundary fake while covering their branches.
    from voice_pipeline import cli

    cli.generate_reference(
        "http://control", ref_request, tmp_path / "direct-reference.wav", True, 3
    )
    cli.generate_gsv("http://control", gsv_request, tmp_path / "direct-target.wav", True, 3)
    cli.synthesize_segment("http://control", segment_request, tmp_path / "direct-segment", True, 3)
