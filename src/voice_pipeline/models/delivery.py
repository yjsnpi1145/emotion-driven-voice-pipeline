from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from voice_pipeline.models.schemas import StrictModel


class Batch1AcceptanceReceipt(StrictModel):
    """Machine-readable Batch 1 handoff, including an explicit listening waiver."""

    schema_version: Literal[1]
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    engineering_disposition: Literal["PASS"]
    golden_listening: Literal["PASS", "waived_by_user"]
    waiver_reason: str | None = None

    @model_validator(mode="after")
    def validate_waiver_reason(self) -> Batch1AcceptanceReceipt:
        if self.golden_listening == "waived_by_user":
            if self.waiver_reason is None or not self.waiver_reason.strip():
                raise ValueError("waiver_reason is required when golden listening is waived")
        return self
