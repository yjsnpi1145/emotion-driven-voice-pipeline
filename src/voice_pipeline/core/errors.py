from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONTROL_PLANE_UNAVAILABLE = "CONTROL_PLANE_UNAVAILABLE"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INDEX_ENGINE_ERROR = "INDEX_ENGINE_ERROR"
    INDEX_TIMEOUT = "INDEX_TIMEOUT"
    GSV_ENGINE_ERROR = "GSV_ENGINE_ERROR"
    GSV_TIMEOUT = "GSV_TIMEOUT"
    INVALID_AUDIO = "INVALID_AUDIO"
    AUDIO_SILENT = "AUDIO_SILENT"
    REFERENCE_DURATION_OUT_OF_RANGE = "REFERENCE_DURATION_OUT_OF_RANGE"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    MODEL_PROFILE_NOT_FOUND = "MODEL_PROFILE_NOT_FOUND"
    MODEL_PROFILE_UNAVAILABLE = "MODEL_PROFILE_UNAVAILABLE"
    MODEL_IMPORT_INVALID = "MODEL_IMPORT_INVALID"
    MODEL_IMPORT_FAILED = "MODEL_IMPORT_FAILED"
    MODEL_SWITCH_FAILED = "MODEL_SWITCH_FAILED"


class PipelineError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        message: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
        requires_engine_abort: bool = False,
        poison_queue: bool = False,
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.requires_engine_abort = requires_engine_abort
        self.poison_queue = poison_queue

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
