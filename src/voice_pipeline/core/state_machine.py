from __future__ import annotations

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import JobStatus

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}


def require_transition(before: JobStatus, after: JobStatus) -> None:
    if after not in _ALLOWED[before]:
        raise PipelineError(
            ErrorCode.JOB_STATE_CONFLICT,
            "job_state",
            f"illegal job transition: {before} -> {after}",
            retryable=False,
            details={"before": before, "after": after},
        )
