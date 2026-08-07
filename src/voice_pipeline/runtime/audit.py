from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from voice_pipeline.models.schemas import EngineFingerprint, WorkerName


class EngineAuditWriter:
    """Append-only JSONL audit log owned by the single control process."""

    def __init__(self, runtime_dir: Path) -> None:
        self.instance_id = uuid4()
        self._log_dir = runtime_dir / "logs" / str(self.instance_id)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._log_dir / "engine-audit.jsonl"
        self._lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        return self._path

    def write(
        self,
        *,
        job_id: UUID,
        request_id: UUID,
        engine: WorkerName,
        event: str,
        engine_pid: int | None = None,
        engine_create_time: float | None = None,
        target_text_sha256_or_null: str | None = None,
        reference_sha256_or_null: str | None = None,
        engine_fingerprint: EngineFingerprint | None = None,
    ) -> None:
        row = {
            "schema_version": 1,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "instance_id": str(self.instance_id),
            "job_id": str(job_id),
            "request_id": str(request_id),
            "engine": engine,
            "event": event,
            "engine_pid": engine_pid,
            "engine_create_time": engine_create_time,
            "target_text_sha256_or_null": target_text_sha256_or_null,
            "reference_sha256_or_null": reference_sha256_or_null,
            "engine_fingerprint": (
                engine_fingerprint.model_dump(mode="json")
                if engine_fingerprint is not None
                else None
            ),
            "monotonic_time": time.monotonic(),
        }
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
