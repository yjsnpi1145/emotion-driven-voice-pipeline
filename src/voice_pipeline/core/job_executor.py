from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.persistence import JobSuccessCommit, JsonValue, PersistentJobRecord
from voice_pipeline.models.schemas import (
    ExecutionContext,
    GsvJobRequest,
    GsvSynthesisRequest,
    GsvSynthesisResult,
    ReferenceJobRequest,
    ReferenceSynthesisResult,
    SegmentSynthesisRequest,
    SegmentSynthesisResult,
)
from voice_pipeline.modules.audio.atomic_output import atomic_write_json
from voice_pipeline.modules.quality.models import QualityReport
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.version_store import VersionCommitService, VersionStore


class JobExecutor:
    """Rebuild one engine request exclusively from its durable snapshot."""

    def __init__(
        self,
        service: SynthesisService,
        *,
        jobs_root: Path,
        artifacts: ArtifactStore | None = None,
        versions: VersionStore | None = None,
        commits: VersionCommitService | None = None,
    ) -> None:
        self._service = service
        self._jobs_root = jobs_root.resolve()
        self._artifacts = artifacts
        self._versions = versions
        self._commits = commits

    async def execute(self, record: PersistentJobRecord) -> JobSuccessCommit:
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
                result = await self._execute_gsv(record, context)
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
        serialized = serialize_synthesis_result(result)
        if record.task_snapshot is None:
            return JobSuccessCommit(result=serialized)
        return await self._commit_segment_result(record, result, serialized)

    async def _execute_gsv(self, record: PersistentJobRecord, context: ExecutionContext) -> Any:
        if record.task_snapshot is None:
            return await self._service.generate_gsv(
                context,
                GsvJobRequest.model_validate(record.request_snapshot),
            )
        if self._versions is None:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "jobs",
                "segment version store is not configured",
                retryable=False,
            )
        reference_version_id = record.task_snapshot.active_ref_version_id
        if reference_version_id is None:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "jobs",
                "segment GSV snapshot has no frozen reference version",
                retryable=False,
            )
        reference_version = await self._versions.get_version(reference_version_id)
        quality = QualityReport.model_validate(reference_version.quality_result)
        frozen = GsvSynthesisRequest.model_validate(record.request_snapshot)
        manifest_path = self._jobs_root / f".{record.job_id}.reference-input.json"
        atomic_write_json(
            manifest_path,
            {
                "schema_version": 1,
                "reference": frozen.reference.model_dump(mode="json"),
                "quality_result": quality.model_dump(mode="json"),
            },
        )
        request = GsvJobRequest(
            request_id=frozen.request_id,
            reference_manifest_path=manifest_path,
            target_text=frozen.text,
            target_language=frozen.text_lang,
            speed_factor=frozen.speed_factor,
            seed=frozen.seed,
            model_profile_id=frozen.model_profile_id,
        )
        try:
            return await self._service.generate_gsv(context, request)
        finally:
            manifest_path.unlink(missing_ok=True)

    async def _commit_segment_result(
        self,
        record: PersistentJobRecord,
        result: Any,
        serialized: dict[str, JsonValue],
    ) -> JobSuccessCommit:
        if self._artifacts is None or self._versions is None or self._commits is None:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "jobs",
                "segment version commit service is not configured",
                retryable=False,
            )
        snapshot = record.task_snapshot
        if snapshot is None:  # pragma: no cover - checked by caller
            raise AssertionError("segment snapshot was lost")
        if isinstance(result, ReferenceSynthesisResult):
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            quality = QualityReport.model_validate(manifest["quality_result"])
            blob = self._artifacts.publish_blob(
                self._artifacts.stage_audio(record.job_id, result.reference.audio.path)
            )
            committed = await self._commits.commit_reference(
                job=record,
                blob=blob,
                reference=result.reference,
                quality=quality,
                activate_on_success=snapshot.activate_on_success,
                result=serialized,
            )
        elif isinstance(result, GsvSynthesisResult):
            reference_version_id = snapshot.active_ref_version_id
            if reference_version_id is None:  # pragma: no cover - submit invariant
                raise AssertionError("GSV job lost frozen reference version")
            reference_version = await self._versions.get_version(reference_version_id)
            quality = QualityReport.model_validate(reference_version.quality_result)
            blob = self._artifacts.publish_blob(
                self._artifacts.stage_audio(record.job_id, result.target.path)
            )
            committed = await self._commits.commit_gsv(
                job=record,
                blob=blob,
                reference_version=reference_version,
                quality=quality,
                model_fingerprint=record.model_fingerprint,
                activate_on_success=snapshot.activate_on_success,
                result=serialized,
            )
        else:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "jobs",
                "only independent reference and GSV jobs can create one segment version",
                retryable=False,
            )
        return JobSuccessCommit(
            result=serialized,
            activation_outcome=committed.activation_outcome,
            artifact_version_ids=(committed.version.version_id,),
            terminal_committed=True,
        )


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
