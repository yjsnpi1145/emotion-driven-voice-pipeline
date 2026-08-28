from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.core.reference_duration_probe import ServiceReferenceDurationProbe
from voice_pipeline.core.role_preset_service import RolePresetService
from voice_pipeline.core.segment_job_service import SegmentJobService
from voice_pipeline.models.director import (
    DirectorGenerationItemRecord,
    DirectorGenerationRecord,
    DirectorProjectRecord,
    DirectorRoleRecord,
    DirectorUtteranceRecord,
    RolePresetRecord,
)
from voice_pipeline.models.persistence import (
    CreateDubbingTaskRequest,
    CreateSegmentRequest,
    OutputAudioSpec,
    SegmentGsvJobRequest,
    SegmentInputsPatch,
    SegmentRecord,
    SegmentReferenceJobRequest,
)
from voice_pipeline.modules.audio.composer import ComposeInput, compose_final
from voice_pipeline.modules.llm.director import ReferenceTextDirector
from voice_pipeline.modules.llm.models import DirectedSegment
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.director_store import DirectorStore
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.segment_store import SegmentStore
from voice_pipeline.storage.version_store import VersionStore


class DirectorGenerationService:
    """Fault-isolated two-phase Index-first then model-grouped GSV scheduler."""

    def __init__(
        self,
        *,
        directors: DirectorStore,
        presets: RolePresetService,
        segments: SegmentStore,
        jobs: SqliteJobStore,
        segment_jobs: SegmentJobService,
        versions: VersionStore,
        artifacts: ArtifactStore,
        director: Any,
        synthesis: SynthesisService,
        queue: SerialGpuQueue,
        jobs_root: Path,
        max_reference_corrections: int,
        notify_jobs: Callable[[], Awaitable[None]],
    ) -> None:
        self._directors = directors
        self._presets = presets
        self._segments = segments
        self._jobs = jobs
        self._segment_jobs = segment_jobs
        self._versions = versions
        self._artifacts = artifacts
        self._director = director
        self._synthesis = synthesis
        self._queue = queue
        self._jobs_root = jobs_root
        self._max_reference_corrections = max_reference_corrections
        self._notify_jobs = notify_jobs
        self._active: dict[UUID, asyncio.Task[None]] = {}

    async def start(self, project_id: UUID, *, expected_revision: int) -> DirectorGenerationRecord:
        project = await self._directors.get_project(project_id)
        current = await self._directors.current_generation(project_id)
        if current is not None and current.project_revision == expected_revision:
            return current
        if project.revision != expected_revision or project.status != "ready":
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director_generation",
                "project is not ready at the submitted revision",
                retryable=False,
            )
        utterances = [
            item for item in await self._directors.list_utterances(project_id) if item.speak_enabled
        ]
        roles = {item.role_id: item for item in await self._directors.list_roles(project_id)}
        preset_by_role: dict[UUID, RolePresetRecord] = {}
        mapped_utterances: list[DirectorUtteranceRecord] = []
        for utterance in utterances:
            if utterance.role_id is None or utterance.role_id not in roles:
                raise _preset_blocker("spoken utterance has no role", utterance)
            role = roles[utterance.role_id]
            if not role.dubbing_enabled:
                continue
            if role.preset_id is None:
                raise _preset_blocker("spoken role has no role preset", utterance)
            preset = await self._presets.resolve(role.preset_id)
            if preset.status != "ready":
                raise _preset_blocker("spoken role preset is unavailable", utterance)
            preset_by_role[role.role_id] = preset
            mapped_utterances.append(utterance)
        utterances = mapped_utterances
        if not utterances:
            raise PipelineError(
                ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                "director_generation",
                "project has no mapped spoken utterances",
                retryable=False,
            )
        adjusted_utterances: list[DirectorUtteranceRecord] = []
        for utterance in utterances:
            role_id = utterance.role_id
            resolved_preset = preset_by_role.get(role_id) if role_id is not None else None
            if resolved_preset is None:
                raise _preset_blocker("spoken utterance has no usable role preset", utterance)
            adjusted_utterances.append(
                utterance.model_copy(
                    update={
                        "speed_factor": (
                            resolved_preset.default_speed
                            if utterance.speed_factor == 1.0
                            else utterance.speed_factor
                        )
                    }
                )
            )
        utterances = adjusted_utterances
        snapshot = {
            "schema_version": 1,
            "project": project.model_dump(mode="json"),
            "utterances": [item.model_dump(mode="json") for item in utterances],
            "roles": [item.model_dump(mode="json") for item in roles.values()],
            "presets": [item.model_dump(mode="json") for item in preset_by_role.values()],
        }
        generation = await self._directors.prepare_generation(
            project_id,
            expected_revision=expected_revision,
            snapshot=snapshot,
            items=tuple(
                (
                    utterance.utterance_id,
                    ordinal,
                    preset_by_role[utterance.role_id].model_profile_id,  # type: ignore[index]
                )
                for ordinal, utterance in enumerate(utterances)
            ),
        )
        task = asyncio.create_task(
            self._run(generation.generation_id, project, utterances, preset_by_role)
        )
        self._active[generation.generation_id] = task
        task.add_done_callback(lambda completed: self._forget(generation.generation_id, completed))
        return generation

    async def get_for_project(
        self, project_id: UUID
    ) -> tuple[DirectorGenerationRecord | None, list[DirectorGenerationItemRecord]]:
        generation = await self._directors.current_generation(project_id)
        if generation is None:
            return None, []
        return generation, await self._directors.list_generation_items(generation.generation_id)

    async def resume(self, project_id: UUID, *, expected_revision: int) -> DirectorGenerationRecord:
        project = await self._directors.get_project(project_id)
        if project.revision != expected_revision:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "director_generation",
                "director data changed; refresh before resuming",
                retryable=False,
            )
        generation = await self._directors.current_generation(project_id)
        if generation is None:
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director_generation",
                "project has no generation to resume",
                retryable=False,
            )
        active = self._active.get(generation.generation_id)
        if active is not None and not active.done():
            return generation
        if generation.status == "succeeded":
            return generation
        snapshot_project, utterances, preset_by_role = _snapshot_inputs(generation)
        current_utterances = {
            item.utterance_id: item for item in await self._directors.list_utterances(project_id)
        }
        for item in await self._directors.list_generation_items(generation.generation_id):
            current = current_utterances[item.utterance_id]
            if current.segment_id is not None:
                segment = await self._segments.get_segment(current.segment_id)
                if segment.active_gsv_version_id is not None:
                    await self._directors.attach_utterance_versions(
                        item.utterance_id,
                        gsv_version_id=segment.active_gsv_version_id,
                    )
                    status = "ready"
                elif segment.active_ref_version_id is not None:
                    await self._directors.attach_utterance_versions(
                        item.utterance_id,
                        reference_version_id=segment.active_ref_version_id,
                    )
                    status = "reference_ready"
                else:
                    status = "queued"
            else:
                status = "queued"
            await self._directors.set_generation_item(
                generation.generation_id,
                item.utterance_id,
                status=status,
            )
        generation = await self._directors.reopen_generation(
            project_id,
            generation.generation_id,
            expected_revision=expected_revision,
        )
        task = asyncio.create_task(
            self._run(generation.generation_id, snapshot_project, utterances, preset_by_role)
        )
        self._active[generation.generation_id] = task
        task.add_done_callback(lambda completed: self._forget(generation.generation_id, completed))
        return generation

    async def recompose(
        self, project_id: UUID, *, expected_revision: int
    ) -> DirectorGenerationRecord:
        project = await self._directors.get_project(project_id)
        if project.revision != expected_revision:
            raise PipelineError(
                ErrorCode.VERSION_CONFLICT,
                "director_generation",
                "director data changed; refresh before recomposing",
                retryable=False,
            )
        generation = await self._directors.current_generation(project_id)
        if generation is None:
            raise PipelineError(
                ErrorCode.DIRECTOR_STATE_CONFLICT,
                "director_generation",
                "project has no generation to compose",
                retryable=False,
            )
        _, utterances, _ = _snapshot_inputs(generation)
        current = {
            item.utterance_id: item for item in await self._directors.list_utterances(project_id)
        }
        materialized: dict[UUID, UUID] = {}
        for utterance in utterances:
            record = current[utterance.utterance_id]
            if record.segment_id is None:
                raise PipelineError(
                    ErrorCode.VERSION_NOT_READY,
                    "director_generation",
                    "a spoken utterance has not been materialized",
                    retryable=False,
                )
            segment = await self._segments.get_segment(record.segment_id)
            if segment.active_gsv_version_id is None:
                raise PipelineError(
                    ErrorCode.VERSION_NOT_READY,
                    "director_generation",
                    "a spoken utterance has no ready GSV version",
                    retryable=False,
                )
            materialized[utterance.utterance_id] = record.segment_id
            await self._directors.attach_utterance_versions(
                utterance.utterance_id,
                gsv_version_id=segment.active_gsv_version_id,
            )
            await self._directors.set_generation_item(
                generation.generation_id,
                utterance.utterance_id,
                status="ready",
            )
        await self._compose(generation.generation_id, materialized, utterances)
        return await self._directors.get_generation(generation.generation_id)

    async def recover(self) -> tuple[UUID, ...]:
        return await self._directors.mark_running_generations_interrupted()

    async def stop(self, *, deadline: float) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait(tasks, timeout=remaining)

    def _forget(self, generation_id: UUID, completed: asyncio.Future[None]) -> None:
        if self._active.get(generation_id) is completed:
            self._active.pop(generation_id, None)

    async def _run(
        self,
        generation_id: UUID,
        project: DirectorProjectRecord,
        utterances: list[DirectorUtteranceRecord],
        preset_by_role: dict[UUID, RolePresetRecord],
    ) -> None:
        try:
            await self._directors.mark_generation_running(generation_id)
            materialized = await self._materialize(project, utterances)
            item_by_utterance = {
                item.utterance_id: item
                for item in await self._directors.list_generation_items(generation_id)
            }
            # Phase one: no GSV job is submitted until every reference attempt has ended.
            for utterance in utterances:
                item = item_by_utterance[utterance.utterance_id]
                segment_id = materialized[utterance.utterance_id]
                if item.status in {"reference_ready", "ready"}:
                    continue
                preset = preset_by_role[utterance.role_id]  # type: ignore[index]
                try:
                    await self._directors.set_generation_item(
                        generation_id,
                        utterance.utterance_id,
                        status="reference_running",
                    )
                    segment = await self._segments.get_segment(segment_id)
                    segment = await self._resolve_reference_text(
                        segment,
                        utterance,
                        self._presets.audio_path(preset),
                    )
                    context = await self._segment_jobs.submit_reference(
                        segment.segment_id,
                        SegmentReferenceJobRequest(
                            request_id=uuid4(),
                            base_voice_path=self._presets.audio_path(preset),
                        ),
                    )
                    await self._directors.set_generation_item(
                        generation_id,
                        utterance.utterance_id,
                        status="reference_running",
                        reference_job_id=context.job_id,
                    )
                    await self._notify_jobs()
                    await self._await_job(context.job_id)
                    segment = await self._segments.get_segment(segment.segment_id)
                    await self._directors.attach_utterance_versions(
                        utterance.utterance_id,
                        reference_version_id=segment.active_ref_version_id,
                    )
                    await self._directors.set_generation_item(
                        generation_id,
                        utterance.utterance_id,
                        status="reference_ready",
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    await self._directors.set_generation_item(
                        generation_id,
                        utterance.utterance_id,
                        status="failed",
                        error=_error_payload(exc),
                    )

            # Phase two: group by frozen model profile while retaining source ordinals.
            groups: dict[UUID, list[DirectorUtteranceRecord]] = defaultdict(list)
            refreshed = {
                item.utterance_id: item
                for item in await self._directors.list_generation_items(generation_id)
            }
            for utterance in utterances:
                item = refreshed[utterance.utterance_id]
                if item.status == "reference_ready":
                    groups[item.model_profile_id].append(utterance)
            for model_profile_id in sorted(groups, key=str):
                group = groups[model_profile_id]
                for utterance in group:
                    segment_id = materialized[utterance.utterance_id]
                    try:
                        await self._directors.set_generation_item(
                            generation_id,
                            utterance.utterance_id,
                            status="gsv_running",
                        )
                        context = await self._segment_jobs.submit_gsv(
                            segment_id,
                            SegmentGsvJobRequest(
                                request_id=uuid4(),
                                model_profile_id=model_profile_id,
                            ),
                        )
                        await self._directors.set_generation_item(
                            generation_id,
                            utterance.utterance_id,
                            status="gsv_running",
                            gsv_job_id=context.job_id,
                        )
                        await self._notify_jobs()
                        await self._await_job(context.job_id)
                        segment = await self._segments.get_segment(segment_id)
                        await self._directors.attach_utterance_versions(
                            utterance.utterance_id,
                            gsv_version_id=segment.active_gsv_version_id,
                        )
                        await self._directors.set_generation_item(
                            generation_id,
                            utterance.utterance_id,
                            status="ready",
                        )
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        await self._directors.set_generation_item(
                            generation_id,
                            utterance.utterance_id,
                            status="failed",
                            error=_error_payload(exc),
                        )
            items = await self._directors.list_generation_items(generation_id)
            if any(item.status != "ready" for item in items):
                await self._directors.finish_generation(
                    generation_id,
                    succeeded=False,
                    error={
                        "code": "DIRECTOR_ITEMS_FAILED",
                        "stage": "director_generation",
                        "message": "one or more utterances failed; successful utterances were kept",
                        "failed_utterance_ids": [
                            str(item.utterance_id) for item in items if item.status != "ready"
                        ],
                    },
                )
                return
            await self._compose(generation_id, materialized, utterances)
        except asyncio.CancelledError:
            await self._directors.mark_generation_interrupted(
                generation_id,
                error={
                    "code": "JOB_CANCELLED",
                    "stage": "director_generation",
                    "message": "director generation was interrupted",
                },
            )
            raise
        except BaseException as exc:
            await self._directors.finish_generation(
                generation_id,
                succeeded=False,
                error=_error_payload(exc),
            )

    async def _resolve_reference_text(
        self,
        segment: SegmentRecord,
        utterance: DirectorUtteranceRecord,
        base_voice: Path,
    ) -> SegmentRecord:
        resolver = ReferenceTextDirector(self._director)
        current = segment
        for conflict_attempt in range(2):
            directed = DirectedSegment(
                ordinal=utterance.ordinal,
                source_start=utterance.source_start,
                source_end=utterance.source_end,
                emotion_description="保持当前情绪向量和表演强度",
                emotion_vector=current.current_emotion_vector,
                synthesis_text=current.synthesis_text,
                ref_text_cn=current.ref_text_cn,
                pause_after_ms=current.pause_after_ms,
                speed_factor=current.speed_factor,
                seed=current.seed,
            )
            resolved = await resolver.resolve_reference_text(
                directed,
                ServiceReferenceDurationProbe(
                    synthesis=self._synthesis,
                    queue=self._queue,
                    jobs_root=self._jobs_root / "director-reference-probes",
                    base_voice=base_voice,
                ),
                max_corrections=getattr(
                    self._director,
                    "max_reference_corrections",
                    self._max_reference_corrections,
                ),
            )
            if resolved.ref_text_cn == current.ref_text_cn:
                return current
            try:
                return await self._segments.patch_inputs(
                    current.segment_id,
                    SegmentInputsPatch(
                        expected_ref_draft_revision=current.ref_draft_revision,
                        expected_gsv_draft_revision=current.gsv_draft_revision,
                        ref_text_cn=resolved.ref_text_cn,
                    ),
                )
            except PipelineError as exc:
                if exc.code != ErrorCode.VERSION_CONFLICT:
                    raise
                current = await self._segments.get_segment(current.segment_id)
                if conflict_attempt == 1:
                    return current
        raise AssertionError("reference resolution loop must return or raise")

    async def _materialize(
        self, project: DirectorProjectRecord, utterances: list[DirectorUtteranceRecord]
    ) -> dict[UUID, UUID]:
        existing = {
            item.utterance_id: item.segment_id
            for item in await self._directors.list_utterances(project.project_id)
            if item.speak_enabled and item.segment_id is not None
        }
        if len(existing) == len(utterances):
            return {key: value for key, value in existing.items() if value is not None}
        task = await self._segments.create_task(
            CreateDubbingTaskRequest(
                title=project.title,
                source_text=project.preprocessed_text or project.source_text,
                target_language=project.target_language,
                output_spec=OutputAudioSpec(),
            )
        )
        mapping: dict[UUID, UUID] = {}
        for ordinal, utterance in enumerate(utterances):
            if (
                utterance.synthesis_text is None
                or utterance.ref_text_cn is None
                or utterance.emotion_vector is None
            ):
                raise PipelineError(
                    ErrorCode.DIRECTOR_REVIEW_REQUIRED,
                    "director_generation",
                    "spoken utterance lacks reviewed synthesis inputs",
                    retryable=False,
                )
            segment = await self._segments.create_segment(
                task.task_id,
                CreateSegmentRequest(
                    ordinal=ordinal,
                    source_start=utterance.source_start,
                    source_end=utterance.source_end,
                    source_text=utterance.source_text,
                    synthesis_text=utterance.synthesis_text,
                    llm_emotion_vector=utterance.emotion_vector,
                    ref_text_cn=utterance.ref_text_cn,
                    speed_factor=utterance.speed_factor,
                    pause_after_ms=utterance.pause_after_ms,
                    seed=utterance.seed,
                ),
            )
            await self._directors.attach_materialized_segment(
                utterance.utterance_id,
                task_id=task.task_id,
                segment_id=segment.segment_id,
            )
            mapping[utterance.utterance_id] = segment.segment_id
        return mapping

    async def _await_job(self, job_id: UUID) -> None:
        while True:
            record = await self._jobs.get(job_id)
            if record.status == "succeeded":
                return
            if record.status in {"failed", "cancelled", "interrupted"}:
                error = record.error or {}
                raise PipelineError(
                    ErrorCode.ENGINE_UNAVAILABLE,
                    "director_generation",
                    str(error.get("message", "utterance generation failed")),
                    retryable=bool(error.get("retryable", False)),
                    details={"job_id": str(job_id), "status": record.status},
                )
            await asyncio.sleep(0.02)

    async def _compose(
        self,
        generation_id: UUID,
        materialized: dict[UUID, UUID],
        utterances: list[DirectorUtteranceRecord],
    ) -> None:
        inputs: list[ComposeInput] = []
        for ordinal, utterance in enumerate(utterances):
            segment = await self._segments.get_segment(materialized[utterance.utterance_id])
            if segment.active_gsv_version_id is None:
                raise PipelineError(
                    ErrorCode.VERSION_NOT_READY,
                    "director_generation",
                    "a spoken utterance has no ready GSV version",
                    retryable=False,
                )
            version = await self._versions.get_version(segment.active_gsv_version_id)
            inputs.append(
                ComposeInput(
                    ordinal=ordinal,
                    segment_id=segment.segment_id,
                    gsv_version_id=version.version_id,
                    gsv_content_sha256=version.blob_sha256,
                    blob_path=(self._artifacts.root / version.blob_relative_path).resolve(),
                    pause_after_ms=segment.pause_after_ms,
                    state=version.state,
                )
            )
        output_dir = self._artifacts.root / "directors" / str(generation_id)
        if (output_dir / "final.wav").exists():
            output_dir = output_dir / "recompositions" / str(uuid4())
        composed = compose_final(
            ordered_inputs=tuple(inputs),
            output_spec=OutputAudioSpec(),
            output_path=output_dir / "final.wav",
            timeline_path=output_dir / "timeline.json",
        )
        await self._directors.finish_generation(
            generation_id,
            succeeded=True,
            final_relative_path=composed.audio.path.relative_to(self._artifacts.root).as_posix(),
            timeline=composed.timeline.model_dump(mode="json"),
        )


def _preset_blocker(message: str, utterance: DirectorUtteranceRecord) -> PipelineError:
    return PipelineError(
        ErrorCode.ROLE_PRESET_UNAVAILABLE,
        "director_generation",
        message,
        retryable=False,
        details={"utterance_id": str(utterance.utterance_id)},
    )


def _error_payload(error: BaseException) -> dict[str, Any]:
    if isinstance(error, PipelineError):
        return error.as_dict()
    return {
        "code": "INTERNAL_ERROR",
        "stage": "director_generation",
        "message": "director generation failed",
        "retryable": False,
        "details": {},
    }


def _snapshot_inputs(
    generation: DirectorGenerationRecord,
) -> tuple[
    DirectorProjectRecord,
    list[DirectorUtteranceRecord],
    dict[UUID, RolePresetRecord],
]:
    try:
        project = DirectorProjectRecord.model_validate(generation.snapshot["project"])
        utterances = [
            DirectorUtteranceRecord.model_validate(item)
            for item in generation.snapshot["utterances"]  # type: ignore[union-attr]
        ]
        roles = {
            role.role_id: role
            for role in (
                DirectorRoleRecord.model_validate(item)
                for item in generation.snapshot["roles"]  # type: ignore[union-attr]
            )
        }
        presets = {
            preset.preset_id: preset
            for preset in (
                RolePresetRecord.model_validate(item)
                for item in generation.snapshot["presets"]  # type: ignore[union-attr]
            )
        }
        preset_by_role = {
            role_id: presets[role.preset_id]
            for role_id, role in roles.items()
            if role.preset_id is not None
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(
            ErrorCode.INVALID_INPUT,
            "director_generation",
            "director generation snapshot is invalid",
            retryable=False,
        ) from exc
    return project, utterances, preset_by_role
