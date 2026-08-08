from __future__ import annotations

from pathlib import Path
from typing import Any

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.persistence import JsonValue, PersistentJobRecord
from voice_pipeline.models.schemas import (
    ExecutionContext,
    GsvJobRequest,
    GsvSynthesisResult,
    ReferenceJobRequest,
    ReferenceSynthesisResult,
    SegmentSynthesisRequest,
    SegmentSynthesisResult,
)


class JobExecutor:
    """Rebuild one engine request exclusively from its durable snapshot."""

    def __init__(self, service: SynthesisService, *, jobs_root: Path) -> None:
        self._service = service
        self._jobs_root = jobs_root.resolve()

    async def execute(self, record: PersistentJobRecord) -> dict[str, JsonValue]:
        context = ExecutionContext(
            job_id=record.job_id,
            request_id=record.request_id,
            job_dir=self._jobs_root / str(record.job_id),
        )
        try:
            result: Any
            if record.kind == "reference":
                result = await self._service.generate_reference(
                    context,
                    ReferenceJobRequest.model_validate(record.request_snapshot),
                )
            elif record.kind == "gsv":
                result = await self._service.generate_gsv(
                    context,
                    GsvJobRequest.model_validate(record.request_snapshot),
                )
            elif record.kind == "segment":
                result = await self._service.synthesize_segment(
                    context,
                    SegmentSynthesisRequest.model_validate(record.request_snapshot),
                )
            else:  # pragma: no cover - PersistentJobRecord constrains this
                raise AssertionError(f"unsupported job kind: {record.kind}")
        except ValueError as exc:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "jobs",
                "stored job request snapshot is invalid",
                retryable=False,
            ) from exc
        return serialize_synthesis_result(result)


def serialize_synthesis_result(result: Any) -> dict[str, JsonValue]:
    if isinstance(result, ReferenceSynthesisResult):
        return {
            "job_id": str(result.job_id),
            "request_id": str(result.request_id),
            "kind": "reference",
            "reference": result.reference.model_dump(mode="json"),
            "manifest_path": str(result.manifest_path),
        }
    if isinstance(result, GsvSynthesisResult):
        return {
            "job_id": str(result.job_id),
            "request_id": str(result.request_id),
            "kind": "gsv",
            "target": result.target.model_dump(mode="json"),
            "manifest_path": str(result.manifest_path),
            "reference_content_sha256": result.reference_content_sha256,
        }
    if isinstance(result, SegmentSynthesisResult):
        return {
            "job_id": str(result.job_id),
            "request_id": str(result.request_id),
            "kind": "segment",
            "reference": result.reference.model_dump(mode="json"),
            "target": result.target.model_dump(mode="json"),
            "reference_manifest_path": str(result.reference_manifest_path),
            "run_manifest_path": str(result.run_manifest_path),
        }
    raise TypeError(f"unknown synthesis result type: {type(result)}")
