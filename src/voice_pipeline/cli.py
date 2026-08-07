from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import typer
from pydantic import ValidationError

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import (
    GsvJobRequest,
    ReferenceJobRequest,
    SegmentSynthesisRequest,
)
from voice_pipeline.modules.audio.atomic_output import (
    OutputReservation,
    reserve_output_path,
)
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file

app = typer.Typer(add_completion=False, no_args_is_help=True)

_POLL_INTERVAL_SECONDS = 0.25
_DEFAULT_TIMEOUT_SECONDS = 900.0


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _fatal(
    code: str,
    stage: str,
    message: str,
    *,
    exit_code: int,
    details: dict[str, Any] | None = None,
) -> None:
    _emit_json(
        {
            "error": {
                "code": code,
                "stage": stage,
                "message": message,
                "retryable": False,
                "details": details or {},
            }
        }
    )
    raise typer.Exit(exit_code)


def _exit_code_for(exc: PipelineError) -> int:
    if exc.code in (
        ErrorCode.INVALID_INPUT,
        ErrorCode.OUTPUT_CONFLICT,
        ErrorCode.CONFIG_INVALID,
    ):
        return 2
    if exc.code in (
        ErrorCode.CONTROL_PLANE_UNAVAILABLE,
        ErrorCode.ENGINE_UNAVAILABLE,
    ):
        return 3
    if exc.code in (
        ErrorCode.QUEUE_TIMEOUT,
        ErrorCode.INDEX_TIMEOUT,
        ErrorCode.GSV_TIMEOUT,
    ):
        return 5
    return 4


def _load_request(model: type[Any], request_path: Path) -> Any:
    try:
        raw = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fatal(
            ErrorCode.INVALID_INPUT.value,
            "cli",
            f"cannot read request file: {exc}",
            exit_code=2,
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        _fatal(
            ErrorCode.INVALID_INPUT.value,
            "cli",
            "request validation failed",
            exit_code=2,
            details={"errors": exc.errors()[:10]},
        )


def _client(server: str, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=server, timeout=timeout)


def _handle_http_error(resp: httpx.Response, url: str) -> None:
    if resp.status_code in (502, 503, 504):
        raise PipelineError(
            ErrorCode.CONTROL_PLANE_UNAVAILABLE,
            "cli",
            f"control plane unavailable (HTTP {resp.status_code})",
            retryable=True,
        )
    try:
        body = resp.json()
        error = body.get("error") or {}
        code = error.get("code") or ErrorCode.ENGINE_UNAVAILABLE.value
        message = error.get("message") or f"HTTP {resp.status_code} from {url}"
        details = error.get("details") or {}
    except Exception:
        code = ErrorCode.ENGINE_UNAVAILABLE.value
        message = f"HTTP {resp.status_code} from {url}"
        details = {}
    raise PipelineError(
        ErrorCode(code),
        "cli",
        message,
        retryable=False,
        details=details,
    )


def _poll_job(client: httpx.Client, job_id: str, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        resp = client.get(f"/api/v1/jobs/{job_id}")
        if resp.status_code == 200:
            status = resp.json()
            if status["status"] in ("succeeded", "failed"):
                return cast("dict[str, Any]", status)
        if time.monotonic() > deadline:
            raise PipelineError(
                ErrorCode.QUEUE_TIMEOUT,
                "cli",
                "job did not finish within timeout",
                retryable=True,
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def _fail_from_status(status: dict[str, Any]) -> None:
    error = status.get("error") or {
        "code": "ENGINE_UNAVAILABLE",
        "stage": "cli",
        "message": f"job ended in {status.get('status')}",
        "retryable": False,
        "details": {},
    }
    raise PipelineError(
        ErrorCode(error["code"]),
        "cli",
        error.get("message", "job failed"),
        retryable=bool(error.get("retryable", False)),
        details=error.get("details") or {},
    )


def _download_to_reservation(
    client: httpx.Client,
    url: str,
    reservation: OutputReservation,
    *,
    probe_reference: bool | None,
) -> None:
    partial = reservation.path.with_name(f".{reservation.path.stem}.{uuid.uuid4()}.partial")
    try:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                _handle_http_error(resp, url)
            with open(partial, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        if probe_reference is not None:
            probe_wav(partial, require_reference_window=probe_reference)
        reservation.publish(partial)
    except BaseException:
        reservation.rollback()
        raise
    finally:
        partial.unlink(missing_ok=True)


def _publish_json(reservation: OutputReservation, payload: Any) -> None:
    partial = reservation.path.with_name(f".{reservation.path.stem}.{uuid.uuid4()}.partial")
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        with open(partial, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        reservation.publish(partial)
    except BaseException:
        reservation.rollback()
        raise
    finally:
        partial.unlink(missing_ok=True)


def _fetch_manifest(client: httpx.Client, job_id: str, which: str) -> dict[str, Any]:
    resp = client.get(f"/api/v1/jobs/{job_id}/manifest/{which}")
    if resp.status_code != 200:
        _handle_http_error(resp, f"/api/v1/jobs/{job_id}/manifest/{which}")
    return cast("dict[str, Any]", resp.json())


# ---------------------------------------------------------------------- #
# commands
# ---------------------------------------------------------------------- #


@app.command()
def serve(
    config: Path = typer.Option(..., "--config"),
) -> None:
    import uvicorn

    from voice_pipeline.api.app import create_app
    from voice_pipeline.core.config import load_settings

    try:
        settings = load_settings(config)
    except Exception as exc:
        _fatal(
            ErrorCode.CONFIG_INVALID.value,
            "cli",
            f"invalid config: {exc}",
            exit_code=2,
        )
    app_obj = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(
            app_obj,
            host=settings.server.host,
            port=settings.server.port,
            workers=1,
            reload=False,
        )
    )
    plane = app_obj.state.plane
    plane.set_exit_callback(lambda: setattr(server, "should_exit", True))
    server.run()


@app.command()
def doctor(
    server: str = typer.Option(..., "--server"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        with _client(server, 10.0) as client:
            resp = client.get("/api/v1/health")
            if resp.status_code != 200:
                _handle_http_error(resp, "/api/v1/health")
            payload = resp.json()
    except httpx.HTTPError as exc:
        _fatal(
            ErrorCode.CONTROL_PLANE_UNAVAILABLE.value,
            "cli",
            f"control plane unreachable: {exc}",
            exit_code=3,
        )
    except PipelineError as exc:
        _emit_json({"error": exc.as_dict()})
        raise typer.Exit(_exit_code_for(exc))
    if json_output:
        _emit_json(payload)
    else:
        typer.echo(
            f"status={payload.get('status')} mode={payload.get('mode')} "
            f"lifecycle={payload.get('engine_lifecycle')}"
        )


@app.command()
def generate_reference(
    server: str = typer.Option(..., "--server"),
    request: Path = typer.Option(..., "--request"),
    output: Path = typer.Option(..., "--output"),
    json_output: bool = typer.Option(False, "--json"),
    timeout_seconds: float = typer.Option(_DEFAULT_TIMEOUT_SECONDS, "--timeout-seconds"),
) -> None:
    req = _load_request(ReferenceJobRequest, request)
    target = output.resolve()
    manifest_target = target.with_name(target.stem + ".reference-manifest.json")
    try:
        with _client(server, timeout_seconds) as client:
            submit = client.post("/api/v1/jobs/reference", json=req.model_dump(mode="json"))
            if submit.status_code != 202:
                _handle_http_error(submit, "/api/v1/jobs/reference")
            job_id = submit.json()["job_id"]
            status = _poll_job(client, job_id, timeout_seconds)
            if status["status"] != "succeeded":
                _fail_from_status(status)

            audio_res = reserve_output_path(target)
            try:
                manifest_res = reserve_output_path(manifest_target)
            except BaseException:
                audio_res.rollback()
                raise
            try:
                _download_to_reservation(
                    client,
                    f"/api/v1/jobs/{job_id}/audio/reference",
                    audio_res,
                    probe_reference=True,
                )
                manifest = _fetch_manifest(client, job_id, "reference")
                binding = manifest["reference"]
                if binding["audio"]["content_sha256"] != sha256_file(audio_res.path):
                    raise PipelineError(
                        ErrorCode.INVALID_AUDIO,
                        "cli",
                        "downloaded reference sha256 does not match manifest",
                        retryable=False,
                    )
                binding["audio"]["path"] = str(audio_res.path)
                _publish_json(manifest_res, manifest)
            except BaseException:
                for res in (audio_res, manifest_res):
                    try:
                        res.rollback()
                    except Exception:
                        pass
                raise

            if json_output:
                _emit_json(
                    {
                        "job_id": job_id,
                        "request_id": str(req.request_id),
                        "status": "succeeded",
                        "output": str(audio_res.path),
                        "manifest": str(manifest_res.path),
                    }
                )
    except PipelineError as exc:
        _emit_json({"error": exc.as_dict()})
        raise typer.Exit(_exit_code_for(exc))
    except httpx.HTTPError as exc:
        _fatal(
            ErrorCode.CONTROL_PLANE_UNAVAILABLE.value,
            "cli",
            f"control plane unreachable: {exc}",
            exit_code=3,
        )


@app.command()
def generate_gsv(
    server: str = typer.Option(..., "--server"),
    request: Path = typer.Option(..., "--request"),
    output: Path = typer.Option(..., "--output"),
    json_output: bool = typer.Option(False, "--json"),
    timeout_seconds: float = typer.Option(_DEFAULT_TIMEOUT_SECONDS, "--timeout-seconds"),
) -> None:
    req = _load_request(GsvJobRequest, request)
    target = output.resolve()
    try:
        with _client(server, timeout_seconds) as client:
            submit = client.post("/api/v1/jobs/gsv", json=req.model_dump(mode="json"))
            if submit.status_code != 202:
                _handle_http_error(submit, "/api/v1/jobs/gsv")
            job_id = submit.json()["job_id"]
            status = _poll_job(client, job_id, timeout_seconds)
            if status["status"] != "succeeded":
                _fail_from_status(status)

            audio_res = reserve_output_path(target)
            try:
                _download_to_reservation(
                    client,
                    f"/api/v1/jobs/{job_id}/audio/target",
                    audio_res,
                    probe_reference=False,
                )
            except BaseException:
                try:
                    audio_res.rollback()
                except Exception:
                    pass
                raise

            if json_output:
                _emit_json(
                    {
                        "job_id": job_id,
                        "request_id": str(req.request_id),
                        "status": "succeeded",
                        "output": str(audio_res.path),
                    }
                )
    except PipelineError as exc:
        _emit_json({"error": exc.as_dict()})
        raise typer.Exit(_exit_code_for(exc))
    except httpx.HTTPError as exc:
        _fatal(
            ErrorCode.CONTROL_PLANE_UNAVAILABLE.value,
            "cli",
            f"control plane unreachable: {exc}",
            exit_code=3,
        )


@app.command()
def synthesize_segment(
    server: str = typer.Option(..., "--server"),
    request: Path = typer.Option(..., "--request"),
    output_dir: Path = typer.Option(..., "--output-dir"),
    json_output: bool = typer.Option(False, "--json"),
    timeout_seconds: float = typer.Option(_DEFAULT_TIMEOUT_SECONDS, "--timeout-seconds"),
) -> None:
    req = _load_request(SegmentSynthesisRequest, request)
    out_dir = output_dir.resolve()
    targets = [
        ("reference.wav", "audio", "reference"),
        ("reference-manifest.json", "manifest", "reference"),
        ("target.wav", "audio", "target"),
        ("run-manifest.json", "manifest", "run"),
    ]
    try:
        with _client(server, timeout_seconds) as client:
            submit = client.post("/api/v1/jobs/segment", json=req.model_dump(mode="json"))
            if submit.status_code != 202:
                _handle_http_error(submit, "/api/v1/jobs/segment")
            job_id = submit.json()["job_id"]
            status = _poll_job(client, job_id, timeout_seconds)
            if status["status"] != "succeeded":
                _fail_from_status(status)

            out_dir.mkdir(parents=True, exist_ok=True)
            reservations: list[tuple[str, OutputReservation]] = []
            try:
                for name, _, _ in targets:
                    reservations.append((name, reserve_output_path(out_dir / name)))
            except BaseException:
                for _, reservation in reservations:
                    try:
                        reservation.rollback()
                    except Exception:
                        pass
                raise
            by_name = {name: res for name, res in reservations}
            try:
                for name, kind, which in targets:
                    reservation = by_name[name]
                    if kind == "audio":
                        _download_to_reservation(
                            client,
                            f"/api/v1/jobs/{job_id}/audio/{which}",
                            reservation,
                            probe_reference=(which == "reference"),
                        )
                    else:
                        manifest = _fetch_manifest(client, job_id, which)
                        if name == "reference-manifest.json":
                            binding = manifest["reference"]
                            if binding["audio"]["content_sha256"] != sha256_file(
                                by_name["reference.wav"].path
                            ):
                                raise PipelineError(
                                    ErrorCode.INVALID_AUDIO,
                                    "cli",
                                    "downloaded reference sha256 mismatch",
                                    retryable=False,
                                )
                            binding["audio"]["path"] = str(by_name["reference.wav"].path)
                        _publish_json(reservation, manifest)
            except BaseException:
                for _, reservation in reservations:
                    try:
                        reservation.rollback()
                    except Exception:
                        pass
                raise

            if json_output:
                _emit_json(
                    {
                        "job_id": job_id,
                        "request_id": str(req.request_id),
                        "status": "succeeded",
                        "output_dir": str(out_dir),
                    }
                )
    except PipelineError as exc:
        _emit_json({"error": exc.as_dict()})
        raise typer.Exit(_exit_code_for(exc))
    except httpx.HTTPError as exc:
        _fatal(
            ErrorCode.CONTROL_PLANE_UNAVAILABLE.value,
            "cli",
            f"control plane unreachable: {exc}",
            exit_code=3,
        )
