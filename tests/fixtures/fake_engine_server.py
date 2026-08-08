"""Zero-dependency fake engine HTTP server used by external_test mode.

Standard-library only.  Emulates the IndexTTS2 worker (/v1/synthesize) and the
official GPT-SoVITS /tts endpoint.  A ThreadingHTTPServer lets the
/__control/abort endpoint run on another thread while a synthesis request is
in flight.

Run:
    python tests/fixtures/fake_engine_server.py --engine indextts|gpt_sovits \
        --host 127.0.0.1 --port 0 --ready-file <path> \
        [--expected-fingerprint-json <json>] [--delay-ms 450] [--audit-log <path>]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ACTIVE = 0
_MAX_ACTIVE = 0
_ACTIVE_LOCK = threading.Lock()
_CANCEL = threading.Event()
_AUDIT_LOCK = threading.Lock()
_AUDIT_PATH: Path | None = None
_FINGERPRINT: dict = {}
_DELAY_MS = 450
_ENGINE = "indextts"
_FAILURE = "none"
_FAILURE_LOCK = threading.Lock()


def _log_audit(**fields: object) -> None:
    if _AUDIT_PATH is None:
        return
    row = {
        "pid": os.getpid(),
        "sys.executable": sys.executable,
        "monotonic_enter": fields.get("monotonic_enter"),
        "monotonic_exit": fields.get("monotonic_exit"),
        **{k: v for k, v in fields.items() if k not in ("monotonic_enter", "monotonic_exit")},
    }
    with _AUDIT_LOCK:
        with open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()


def _write_simple_wav(path: Path, seconds: float, sample_rate: int, frequency: float) -> None:
    n = int(seconds * sample_rate)
    data = bytearray()
    for i in range(n):
        sample = int(0.2 * math.sin(2 * math.pi * frequency * i / sample_rate) * 32767)
        data += struct.pack("<h", sample)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.write_bytes(header + bytes(data))


def _frequency() -> float:
    raw = _FINGERPRINT.get("source_revision") or "challenge"
    seed = sum(ord(c) for c in raw)
    return 180.0 + (seed % 40) * 5.0


def _run_synthesis_block(body: dict) -> None:
    """Simulate GPU inference for the configured delay; honour cancellation."""
    deadline = time.monotonic() + _DELAY_MS / 1000.0
    while time.monotonic() < deadline:
        if _CANCEL.is_set():
            raise RuntimeError("aborted by control")
        time.sleep(0.01)


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health/live":
            self._send_json(200, {"status": "alive"})
            return
        if self.path == "/health/ready":
            with _ACTIVE_LOCK:
                active = _ACTIVE
            payload = {
                "state": "ready",
                "pid": os.getpid(),
                "create_time": 0.0,
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "source_revision": _FINGERPRINT.get("source_revision", ""),
                "fingerprint": _FINGERPRINT,
                "preflight_ok": True,
                "active_inference": active,
            }
            self._send_json(200, payload)
            return
        if self.path == "/__control/status":
            with _ACTIVE_LOCK:
                active = _ACTIVE
                maximum = _MAX_ACTIVE
            self._send_json(
                200,
                {"active_inference": active, "max_active_observed": maximum},
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/__control/abort":
            _CANCEL.set()
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                with _ACTIVE_LOCK:
                    if _ACTIVE == 0:
                        break
                time.sleep(0.01)
            self._send_json(
                200,
                {
                    "active_inference": 0,
                    "fingerprint": _FINGERPRINT,
                },
            )
            return
        if self.path == "/__control/configure":
            global _FAILURE, _DELAY_MS
            try:
                cfg = json.loads(body.decode("utf-8"))
                with _FAILURE_LOCK:
                    _FAILURE = cfg.get("failure", _FAILURE)
                    if "delay_ms" in cfg:
                        _DELAY_MS = int(cfg["delay_ms"])
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"failure": _FAILURE, "delay_ms": _DELAY_MS})
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send_json(400, {"error": f"invalid json: {exc}"})
            return
        if self.path == "/v1/synthesize" and _ENGINE == "indextts":
            self._handle_index(payload)
            return
        if self.path == "/tts" and _ENGINE == "gpt_sovits":
            self._handle_gsv(payload)
            return
        self._send_json(404, {"error": "not found"})

    def _handle_index(self, payload: dict) -> None:
        global _ACTIVE, _MAX_ACTIVE
        with _ACTIVE_LOCK:
            _ACTIVE += 1
            _MAX_ACTIVE = max(_MAX_ACTIVE, _ACTIVE)
        enter = time.monotonic()
        request_id = payload.get("request_id", "")
        output_path = payload.get("output_path")
        try:
            with _FAILURE_LOCK:
                failure = _FAILURE
            if failure == "http500":
                raise RuntimeError("configured failure: http500")
            _run_synthesis_block(payload)
            if failure == "no_file":
                raise RuntimeError("configured failure: no output file")
            if not output_path:
                raise RuntimeError("output_path required")
            target = Path(str(output_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            if failure in ("short", "long"):
                _write_simple_wav(
                    target,
                    seconds=2.9 if failure == "short" else 9.1,
                    sample_rate=22050,
                    frequency=_frequency(),
                )
            else:
                _write_simple_wav(target, seconds=4.0, sample_rate=22050, frequency=_frequency())
            with _ACTIVE_LOCK:
                _ACTIVE -= 1
            _log_audit(
                engine="indextts",
                request_id=request_id,
                request=payload,
                monotonic_enter=enter,
                monotonic_exit=time.monotonic(),
            )
            self._send_json(
                200,
                {
                    "request_id": request_id,
                    "output_path": str(target.resolve()),
                    "effective_emotion_vector": list(payload.get("emotion_vector", [])),
                    "engine_fingerprint": _FINGERPRINT,
                },
            )
        except Exception as exc:
            with _ACTIVE_LOCK:
                _ACTIVE = max(0, _ACTIVE - 1)
            _log_audit(
                engine="indextts",
                request_id=request_id,
                error=str(exc),
                monotonic_enter=enter,
                monotonic_exit=time.monotonic(),
            )
            self._send_json(500, {"error": str(exc)})

    def _handle_gsv(self, payload: dict) -> None:
        global _ACTIVE, _MAX_ACTIVE
        with _ACTIVE_LOCK:
            _ACTIVE += 1
            _MAX_ACTIVE = max(_MAX_ACTIVE, _ACTIVE)
        enter = time.monotonic()
        request_id = payload.get("text", "")
        try:
            with _FAILURE_LOCK:
                failure = _FAILURE
            if failure == "http500":
                raise RuntimeError("configured failure: http500")
            _run_synthesis_block(payload)
            if failure == "corrupt":
                with _ACTIVE_LOCK:
                    _ACTIVE -= 1
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", "40")
                self.end_headers()
                self.wfile.write(b"\x00" * 40)
                return
            wav = self._gsv_wav_bytes()
            with _ACTIVE_LOCK:
                _ACTIVE -= 1
            _log_audit(
                engine="gpt_sovits",
                request_id=request_id,
                request=payload,
                reference_sha256=self._extract_reference_sha(payload),
                monotonic_enter=enter,
                monotonic_exit=time.monotonic(),
            )
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        except Exception as exc:
            with _ACTIVE_LOCK:
                _ACTIVE = max(0, _ACTIVE - 1)
            _log_audit(
                engine="gpt_sovits",
                request_id=request_id,
                error=str(exc),
                monotonic_enter=enter,
                monotonic_exit=time.monotonic(),
            )
            self._send_json(500, {"error": str(exc)})

    @staticmethod
    def _extract_reference_sha(payload: dict) -> str | None:
        return None

    def _gsv_wav_bytes(self) -> bytes:
        import io

        buffer = io.BytesIO()
        n = int(1.5 * 32000)
        data = bytearray()
        for i in range(n):
            sample = int(0.2 * math.sin(2 * math.pi * _frequency() * i / 32000) * 32767)
            data += struct.pack("<h", sample)
        header = (
            b"RIFF"
            + struct.pack("<I", 36 + len(data))
            + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 32000, 32000 * 2, 2, 16)
            + b"data"
            + struct.pack("<I", len(data))
        )
        buffer.write(header + bytes(data))
        return buffer.getvalue()


def main() -> None:
    global _FINGERPRINT, _AUDIT_PATH, _DELAY_MS, _ENGINE
    parser = argparse.ArgumentParser(prog="fake_engine_server")
    parser.add_argument("--engine", choices=["indextts", "gpt_sovits"], required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--expected-fingerprint-json", default="{}")
    parser.add_argument("--delay-ms", type=int, default=450)
    parser.add_argument("--audit-log", default="")
    args = parser.parse_args()

    _ENGINE = args.engine
    _DELAY_MS = args.delay_ms
    _FINGERPRINT = json.loads(args.expected_fingerprint_json)
    if args.audit_log:
        _AUDIT_PATH = Path(args.audit_log)
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), FakeHandler)
    host, port = server.server_address[:2]
    ready = {
        "pid": os.getpid(),
        "sys.executable": sys.executable,
        "engine": args.engine,
        "host": host,
        "port": port,
        "fingerprint": _FINGERPRINT,
    }
    ready_path = Path(args.ready_file)
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_text(json.dumps(ready), encoding="utf-8")
    print(f"fake {args.engine} ready on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
