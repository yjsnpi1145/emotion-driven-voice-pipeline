from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from voice_pipeline.models.persistence import (
    ArtifactVersionRecord,
    RetryJobRequest,
    SegmentInputsPatch,
)


def test_retry_mode_is_frozen_snapshot_only() -> None:
    assert RetryJobRequest(mode="frozen_snapshot").mode == "frozen_snapshot"
    with pytest.raises(ValidationError):
        RetryJobRequest(mode="current")  # type: ignore[arg-type]


def test_segment_patch_requires_at_least_one_change() -> None:
    with pytest.raises(ValidationError, match="at least one segment input must change"):
        SegmentInputsPatch(
            expected_ref_draft_revision=1,
            expected_gsv_draft_revision=1,
        )


def test_gsv_version_requires_reference_binding() -> None:
    with pytest.raises(ValidationError, match="reference version and hash"):
        ArtifactVersionRecord(
            version_id=uuid4(),
            segment_id=uuid4(),
            artifact_type="gsv",
            source_job_id=uuid4(),
            blob_sha256="a" * 64,
            manifest_relative_path="manifests/test.json",
            ref_version_id=None,
            ref_content_sha256=None,
            input_snapshot={},
            model_fingerprint={},
            quality_result={},
        )
