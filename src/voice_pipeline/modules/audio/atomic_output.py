from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from voice_pipeline.core.errors import ErrorCode, PipelineError


class OutputReservation:
    """Exclusive zero-byte reservation of an immutable output target.

    The target is created with ``os.O_CREAT | os.O_EXCL | os.O_WRONLY`` so an
    already-existing file can never be silently overwritten.  Only the object
    that created the reservation may publish over it or roll it back.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._active = True

    @property
    def path(self) -> Path:
        return self._path

    def publish(self, partial_path: Path) -> None:
        if not self._active:
            raise PipelineError(
                ErrorCode.OUTPUT_CONFLICT,
                "output",
                f"reservation for {self._path} already published or rolled back",
                retryable=False,
            )
        retry_delays = (0.0, 0.01, 0.025, 0.05, 0.1, 0.2)
        for attempt, delay_seconds in enumerate(retry_delays):
            if delay_seconds:
                time.sleep(delay_seconds)
            # Re-check ownership before every retry so a foreign writer can
            # never turn a transient sharing violation into an overwrite.
            if not self._path.is_file() or self._path.stat().st_size != 0:
                self._active = False
                raise PipelineError(
                    ErrorCode.OUTPUT_CONFLICT,
                    "output",
                    f"reservation ownership lost for {self._path}",
                    retryable=False,
                )
            try:
                os.replace(str(partial_path), str(self._path))
                break
            except PermissionError:
                if attempt == len(retry_delays) - 1:
                    raise
        self._active = False

    def rollback(self) -> None:
        if not self._active:
            raise PipelineError(
                ErrorCode.OUTPUT_CONFLICT,
                "output",
                f"reservation for {self._path} already published or rolled back",
                retryable=False,
            )
        if self._path.is_file() and self._path.stat().st_size == 0:
            self._path.unlink(missing_ok=True)
        self._active = False


def reserve_output_path(path: Path) -> OutputReservation:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PipelineError(
            ErrorCode.OUTPUT_CONFLICT,
            "output",
            f"output target already exists: {target}",
            retryable=False,
        ) from exc
    os.close(fd)
    return OutputReservation(target)


def atomic_write_json(path: Path, payload: Any) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    reservation = reserve_output_path(target)
    partial = target.with_name(f".{target.stem}.{uuid.uuid4()}.partial.json")
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        with open(partial, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        reservation.publish(partial)
    except BaseException:
        reservation.rollback()
        raise
    finally:
        partial.unlink(missing_ok=True)
