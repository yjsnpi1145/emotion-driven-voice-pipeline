from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.model_profiles import ModelProfileSnapshot, ResolvedModelProfile
from voice_pipeline.models.persistence import (
    GsvModelSnapshot,
    SegmentGsvJobRequest,
    SegmentJobSnapshot,
    SegmentReferenceJobRequest,
)
from voice_pipeline.models.schemas import (
    ExecutionContext,
    GsvSynthesisRequest,
    ReferenceBinding,
    ReferenceJobRequest,
)
from voice_pipeline.modules.audio.wav_probe import probe_wav
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.segment_store import SegmentStore
from voice_pipeline.storage.version_store import VersionStore


class SegmentJobService:
    """Freezes one segment revision into a durable reference or GSV execution."""

    def __init__(
        self,
        *,
        jobs: SqliteJobStore,
        segments: SegmentStore,
        versions: VersionStore,
        artifacts: ArtifactStore,
        index: Any,
        gsv: Any,
        model_profile_resolver: (
            Callable[[UUID | None], Awaitable[ResolvedModelProfile]] | None
        ) = None,
        require_model_profile: bool = False,
    ) -> None:
        self._jobs = jobs
        self._segments = segments
        self._versions = versions
        self._artifacts = artifacts
        self._index = index
        self._gsv = gsv
        self._model_profile_resolver = model_profile_resolver
        self._require_model_profile = require_model_profile

    async def submit_reference(
        self, segment_id: UUID, request: SegmentReferenceJobRequest
    ) -> ExecutionContext:
        segment = await self._segments.get_segment(segment_id)
        task = await self._segments.get_task(segment.task_id)
        snapshot = _snapshot(segment, activate_on_success=request.activate_on_success)
        frozen = ReferenceJobRequest(
            request_id=request.request_id,
            base_voice_path=request.base_voice_path,
            ref_text_cn=segment.ref_text_cn,
            emotion_vector=segment.current_emotion_vector,
            seed=segment.seed,
        )
        return await self._jobs.create(
            request_id=request.request_id,
            kind="reference",
            request_snapshot=frozen.model_dump(mode="json"),
            segment_snapshot=snapshot,
            model_fingerprint=self._index.fingerprint().model_dump(mode="json"),
            output_spec=task.output_spec,
        )

    async def submit_gsv(self, segment_id: UUID, request: SegmentGsvJobRequest) -> ExecutionContext:
        segment = await self._segments.get_segment(segment_id)
        task = await self._segments.get_task(segment.task_id)
        if segment.active_ref_version_id is None:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "segments",
                "GSV generation requires an active reference version",
                retryable=False,
            )
        reference_version = await self._versions.get_version(segment.active_ref_version_id)
        if reference_version.artifact_type != "reference" or reference_version.state != "ready":
            raise PipelineError(
                ErrorCode.VERSION_NOT_READY,
                "segments",
                "active reference version is not ready",
                retryable=False,
            )
        binding = self._binding_from_version(reference_version)
        snapshot = _snapshot(segment, activate_on_success=request.activate_on_success)
        selected_profile_id, profile_snapshot = await self._freeze_model_profile(
            request.model_profile_id
        )
        frozen = GsvSynthesisRequest(
            request_id=request.request_id,
            reference=binding,
            text=segment.synthesis_text,
            text_lang=segment.target_language,
            speed_factor=segment.speed_factor,
            seed=segment.seed,
            model_profile_id=selected_profile_id,
        )
        return await self._jobs.create(
            request_id=request.request_id,
            kind="gsv",
            request_snapshot=frozen.model_dump(mode="json"),
            segment_snapshot=snapshot,
            model_fingerprint=self._gsv.fingerprint().model_dump(mode="json"),
            model_profile_snapshot=profile_snapshot,
            output_spec=task.output_spec,
        )

    async def _freeze_model_profile(
        self, profile_id: UUID | None
    ) -> tuple[UUID | None, GsvModelSnapshot | None]:
        if self._model_profile_resolver is None:
            return profile_id, None
        try:
            resolved = await self._model_profile_resolver(profile_id)
        except PipelineError as exc:
            if (
                not self._require_model_profile
                and profile_id is None
                and exc.code == ErrorCode.MODEL_PROFILE_UNAVAILABLE
                and exc.details.get("reason") == "no_active_profile"
            ):
                return None, None
            raise
        profile = ModelProfileSnapshot(
            profile_id=resolved.profile_id,
            display_name=resolved.display_name,
            gpt_relative_path=resolved.gpt_relative_path,
            sovits_relative_path=resolved.sovits_relative_path,
            gpt_sha256=resolved.gpt_sha256,
            sovits_sha256=resolved.sovits_sha256,
        )
        return (
            profile.profile_id,
            GsvModelSnapshot(profile=profile, engine_fingerprint=self._gsv.fingerprint()),
        )

    def _binding_from_version(self, version: object) -> ReferenceBinding:
        from voice_pipeline.models.persistence import ArtifactVersionView

        if not isinstance(
            version, ArtifactVersionView
        ):  # pragma: no cover - static store invariant
            raise TypeError("expected an artifact version view")
        path = (self._artifacts.root / version.blob_relative_path).resolve()
        audio = probe_wav(path, require_reference_window=True)
        if audio.content_sha256 != version.blob_sha256:
            raise PipelineError(
                ErrorCode.ARTIFACT_CORRUPT,
                "segments",
                "active reference blob hash does not match its version",
                retryable=False,
            )
        payload = version.input_snapshot
        try:
            return ReferenceBinding.model_validate(
                {
                    "audio": audio.model_dump(mode="json"),
                    "ref_text_cn": str(payload["ref_text_cn"]),
                    "emotion_vector": payload["emotion_vector"],
                    "base_voice_sha256": str(payload["base_voice_sha256"]),
                    "engine_fingerprint": version.model_fingerprint,
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "segments",
                "active reference version lacks a valid frozen binding",
                retryable=False,
            ) from exc


def _snapshot(segment: object, *, activate_on_success: bool) -> SegmentJobSnapshot:
    from voice_pipeline.models.persistence import SegmentRecord

    if not isinstance(segment, SegmentRecord):  # pragma: no cover - static store invariant
        raise TypeError("expected segment record")
    return SegmentJobSnapshot(
        task_id=segment.task_id,
        segment_id=segment.segment_id,
        ref_draft_revision=segment.ref_draft_revision,
        gsv_draft_revision=segment.gsv_draft_revision,
        selection_revision=segment.selection_revision,
        active_ref_version_id=segment.active_ref_version_id,
        active_gsv_version_id=segment.active_gsv_version_id,
        activate_on_success=activate_on_success,
    )
