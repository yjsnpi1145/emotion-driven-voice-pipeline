from __future__ import annotations

import pytest

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.core.state_machine import require_transition


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "interrupted"),
    ],
)
def test_allows_only_frozen_job_transitions(before: str, after: str) -> None:
    require_transition(before, after)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("succeeded", "running"),
        ("failed", "queued"),
        ("cancelled", "running"),
        ("interrupted", "running"),
        ("queued", "succeeded"),
    ],
)
def test_rejects_illegal_or_reopened_job_transitions(before: str, after: str) -> None:
    with pytest.raises(PipelineError, match="JOB_STATE_CONFLICT"):
        require_transition(before, after)  # type: ignore[arg-type]
