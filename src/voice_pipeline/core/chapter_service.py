from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.core.reference_duration_probe import ServiceReferenceDurationProbe
from voice_pipeline.core.segment_job_service import SegmentJobService
from voice_pipeline.models.chapter import (
    ChapterRunRecord,
    ChapterSegmentProgress,
    ChapterSynthesisRequest,
)
from voice_pipeline.models.model_profiles import ModelProfileSnapshot, ResolvedModelProfile
from voice_pipeline.models.persistence import (
    GsvModelSnapshot,
    OutputAudioSpec,
    SegmentGsvJobRequest,
    SegmentInputsPatch,
    SegmentRecord,
    SegmentReferenceJobRequest,
)
from voice_pipeline.models.schemas import EngineFingerprint, LanguageCode
from voice_pipeline.modules.audio.composer import ComposeInput, compose_final
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.modules.llm.director import ReferenceTextDirector, validate_director_plan
from voice_pipeline.modules.llm.models import (
    CorrectionDirection,
    DirectedSegment,
    DirectorPlan,
)
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.job_store import SqliteJobStore
from voice_pipeline.storage.segment_store import SegmentStore
from voice_pipeline.storage.version_store import VersionStore


class Director(Protocol):
    def create_plan(
        self, *, source_text: str, target_language: LanguageCode
    ) -> Awaitable[DirectorPlan]: ...

    def correct_reference_text(
        self, *, current: str, direction: CorrectionDirection, emotion_description: str
    ) -> Awaitable[str]: ...


class ChapterService:
    """Coordinates a directed chapter through the existing durable segment jobs."""

    def __init__(
        self,
        *,
        chapters: ChapterStore,
        segments: SegmentStore,
        jobs: SqliteJobStore,
        segment_jobs: SegmentJobService,
        versions: VersionStore,
        artifacts: ArtifactStore,
        model_profile_resolver: Callable[[UUID], Awaitable[ResolvedModelProfile]],
        gsv_fingerprint: Callable[[], EngineFingerprint],
        director: Director,
        synthesis: SynthesisService,
        queue: SerialGpuQueue,
        jobs_root: Path,
        max_reference_corrections: int,
        notify_jobs: Callable[[], Awaitable[None]],
    ) -> None:
        self._chapters = chapters
        self._segments = segments
        self._jobs = jobs
        self._segment_jobs = segment_jobs
        self._versions = versions
        self._artifacts = artifacts
        self._model_profile_resolver = model_profile_resolver
        self._gsv_fingerprint = gsv_fingerprint
        self._director = director
        self._synthesis = synthesis
        self._queue = queue
        self._jobs_root = jobs_root.resolve()
        self._max_reference_corrections = max_reference_corrections
        self._notify_jobs = notify_jobs
        self._active: dict[UUID, asyncio.Task[None]] = {}

    async def submit(self, request: ChapterSynthesisRequest) -> ChapterRunRecord:
        base_voice = request.base_voice_path.resolve()
        if not base_voice.is_file() or base_voice.is_symlink():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "chapter",
                "chapter base_voice_path must be an existing regular file",
                retryable=False,
            )
        resolved_profile = await self._model_profile_resolver(request.model_profile_id)
        profile_snapshot = ModelProfileSnapshot(
            profile_id=resolved_profile.profile_id,
            display_name=resolved_profile.display_name,
            gpt_relative_path=resolved_profile.gpt_relative_path,
            sovits_relative_path=resolved_profile.sovits_relative_path,
            gpt_sha256=resolved_profile.gpt_sha256,
            sovits_sha256=resolved_profile.sovits_sha256,
        )
        plan = await self._director.create_plan(
            source_text=request.source_text, target_language=request.target_language
        )
        validate_director_plan(request.source_text, plan)
        run = await self._chapters.create_queued(
            request=request.model_copy(update={"base_voice_path": base_voice}),
            director_plan=plan,
            model_profile_snapshot=GsvModelSnapshot(
                profile=profile_snapshot,
                engine_fingerprint=self._gsv_fingerprint(),
            ).model_dump(mode="json"),
            base_voice_sha256=sha256_file(base_voice),
        )
        task = asyncio.create_task(
            self._run(run.run_id, base_voice, request.model_profile_id, plan)
        )
        self._active[run.run_id] = task
        task.add_done_callback(lambda completed: self._forget_active_task(run.run_id, completed))
        return run

    async def resume(self, run_id: UUID) -> ChapterRunRecord:
        """Claim and continue one repaired failed/interrupted chapter in place."""
        run = await self._chapters.get(run_id)
        request = self._resume_request(run)
        base_voice = self._resume_base_voice(run, request)
        plan = DirectorPlan.model_validate(run.director_plan)
        progress = await self._chapters.progress(run_id)
        self._validate_resume_progress(progress)
        claimed = await self._chapters.mark_resuming(run_id)
        task = asyncio.create_task(
            self._run(
                run_id,
                base_voice,
                request.model_profile_id,
                plan,
                resume=True,
                mark_running=False,
            )
        )
        self._active[run_id] = task
        task.add_done_callback(lambda completed: self._forget_active_task(run_id, completed))
        return claimed

    def _forget_active_task(self, run_id: UUID, completed: asyncio.Future[None]) -> None:
        if self._active.get(run_id) is completed:
            self._active.pop(run_id, None)

    async def get(self, run_id: UUID) -> ChapterRunRecord:
        return await self._chapters.get(run_id)

    async def recover(self) -> tuple[UUID, ...]:
        return await self._chapters.mark_interrupted_running()

    async def recompose(self, run_id: UUID) -> ChapterRunRecord:
        """Explicitly replace a completed run's final audio from current GSV pointers."""
        run = await self._chapters.get(run_id)
        if run.status != "succeeded":
            raise PipelineError(
                ErrorCode.VERSION_NOT_READY,
                "chapter",
                "only a completed chapter can be explicitly recomposed",
                retryable=False,
            )
        return await self._compose(run_id, replace_completed=True)

    async def stop(self, *, deadline: float) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait(tasks, timeout=remaining)

    async def _resolve_reference_text(
        self,
        segment: SegmentRecord,
        planned: DirectedSegment,
        base_voice: Path,
    ) -> SegmentRecord:
        resolver = ReferenceTextDirector(self._director)
        current = segment
        for conflict_attempt in range(2):
            directed = planned.model_copy(
                update={
                    "ref_text_cn": current.ref_text_cn,
                    "emotion_vector": current.current_emotion_vector,
                    "synthesis_text": current.synthesis_text,
                    "pause_after_ms": current.pause_after_ms,
                    "speed_factor": current.speed_factor,
                    "seed": current.seed,
                }
            )
            resolved = await resolver.resolve_reference_text(
                directed,
                ServiceReferenceDurationProbe(
                    synthesis=self._synthesis,
                    queue=self._queue,
                    jobs_root=self._jobs_root / "chapter-reference-probes",
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
                # A user edit made while the background probe was running
                # wins. Re-probe it once instead of publishing stale inputs.
                current = await self._segments.get_segment(current.segment_id)
                if conflict_attempt == 1:
                    return current
        raise AssertionError("reference resolution loop must return or raise")

    async def _run(
        self,
        run_id: UUID,
        base_voice: Path,
        model_profile_id: UUID,
        plan: DirectorPlan,
        *,
        resume: bool = False,
        mark_running: bool = True,
    ) -> None:
        try:
            if mark_running:
                await self._chapters.mark_running(run_id)
            resume_progress = (
                {item.ordinal: item for item in await self._chapters.progress(run_id)}
                if resume
                else {}
            )
            for segment in await self._chapters.list_segments(run_id):
                segment = await self._segments.get_segment(segment.segment_id)
                durable = resume_progress.get(segment.ordinal)
                if durable is not None and durable.gsv_state == "ready":
                    continue
                planned = plan.segments[segment.ordinal]
                if durable is None or durable.reference_state != "ready":
                    if durable is not None:
                        self._raise_if_reference_needs_repair(durable)
                    segment = await self._resolve_reference_text(segment, planned, base_voice)
                    reference = await self._segment_jobs.submit_reference(
                        segment.segment_id,
                        SegmentReferenceJobRequest(request_id=uuid4(), base_voice_path=base_voice),
                    )
                    await self._chapters.set_segment_job(
                        run_id, segment.ordinal, "reference", reference.job_id
                    )
                    await self._notify_jobs()
                    await self._await_job(reference.job_id)
                gsv = await self._segment_jobs.submit_gsv(
                    segment.segment_id,
                    SegmentGsvJobRequest(request_id=uuid4(), model_profile_id=model_profile_id),
                )
                await self._chapters.set_segment_job(run_id, segment.ordinal, "gsv", gsv.job_id)
                await self._notify_jobs()
                await self._await_job(gsv.job_id)
            await self._compose(run_id)
        except asyncio.CancelledError:
            try:
                await self._chapters.mark_failed(
                    run_id,
                    {"code": "JOB_CANCELLED", "stage": "chapter", "message": "chapter cancelled"},
                )
            except KeyError:
                pass
            raise
        except PipelineError as exc:
            await self._chapters.mark_failed(run_id, exc.as_dict())
        except Exception:
            await self._chapters.mark_failed(
                run_id,
                {
                    "code": "INTERNAL_ERROR",
                    "stage": "chapter",
                    "message": "chapter orchestration failed",
                },
            )

    @staticmethod
    def _resume_request(run: ChapterRunRecord) -> ChapterSynthesisRequest:
        raw = run.snapshot.get("request")
        if not isinstance(raw, dict):
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "chapter",
                "chapter snapshot lacks a request object",
                retryable=False,
            )
        try:
            return ChapterSynthesisRequest.model_validate(raw)
        except ValueError as exc:
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "chapter",
                "chapter request snapshot is invalid",
                retryable=False,
            ) from exc

    @staticmethod
    def _resume_base_voice(run: ChapterRunRecord, request: ChapterSynthesisRequest) -> Path:
        stored = request.base_voice_path
        candidate = stored.resolve()
        if stored.is_symlink() or not candidate.is_file():
            raise PipelineError(
                ErrorCode.CHAPTER_STATE_CONFLICT,
                "chapter",
                "章节参考音色不可用，无法继续生成",
                retryable=False,
                details={"action_required": "restore_base_voice"},
            )
        if sha256_file(candidate) != run.base_voice_sha256:
            raise PipelineError(
                ErrorCode.CHAPTER_STATE_CONFLICT,
                "chapter",
                "章节参考音色已发生变化，无法继续生成",
                retryable=False,
                details={"action_required": "restore_base_voice"},
            )
        return candidate

    @classmethod
    def _validate_resume_progress(cls, progress: tuple[ChapterSegmentProgress, ...]) -> None:
        for item in progress:
            if item.gsv_state == "ready":
                continue
            if item.reference_state == "ready":
                return
            cls._raise_if_reference_needs_repair(item)
            return

    @staticmethod
    def _raise_if_reference_needs_repair(progress: ChapterSegmentProgress) -> None:
        status = progress.reference_job_status
        if status is None and progress.reference_state == "missing":
            return
        ordinal = progress.ordinal + 1
        if status in {"queued", "running"}:
            message = f"第 {ordinal} 个分块的参考音频仍在生成，请等待完成后再继续章节"
            action = "wait_for_reference"
        else:
            message = f"第 {ordinal} 个分块的参考音频尚未修好，请先重新生成参考音频"
            action = "regenerate_reference"
        raise PipelineError(
            ErrorCode.CHAPTER_STATE_CONFLICT,
            "chapter",
            message,
            retryable=False,
            details={
                "ordinal": progress.ordinal,
                "segment_id": str(progress.segment_id),
                "action_required": action,
                "reference_state": progress.reference_state,
                "reference_job_status": status,
            },
        )

    async def _await_job(self, job_id: UUID) -> None:
        while True:
            record = await self._jobs.get(job_id)
            if record.status == "succeeded":
                return
            if record.status in {"failed", "cancelled", "interrupted"}:
                error = record.error or {}
                raw_code = str(error.get("code", ErrorCode.ENGINE_UNAVAILABLE.value))
                try:
                    code = ErrorCode(raw_code)
                except ValueError:
                    code = ErrorCode.ENGINE_UNAVAILABLE
                raise PipelineError(
                    code,
                    "chapter",
                    str(error.get("message", "segment generation did not succeed")),
                    retryable=bool(error.get("retryable", False)),
                    details={"job_id": str(job_id), "status": record.status},
                )
            await asyncio.sleep(0.01)

    async def _compose(self, run_id: UUID, *, replace_completed: bool = False) -> ChapterRunRecord:
        run = await self._chapters.get(run_id)
        inputs: list[ComposeInput] = []
        for segment in await self._chapters.list_segments(run_id):
            if segment.active_gsv_version_id is None:
                raise PipelineError(
                    ErrorCode.VERSION_NOT_READY,
                    "chapter",
                    "chapter segment has no active GSV version",
                    retryable=False,
                    details={"ordinal": segment.ordinal, "segment_id": str(segment.segment_id)},
                )
            version = await self._versions.get_version(segment.active_gsv_version_id)
            inputs.append(
                ComposeInput(
                    ordinal=segment.ordinal,
                    segment_id=segment.segment_id,
                    gsv_version_id=version.version_id,
                    gsv_content_sha256=version.blob_sha256,
                    blob_path=(self._artifacts.root / version.blob_relative_path).resolve(),
                    pause_after_ms=segment.pause_after_ms,
                    state=version.state,
                )
            )
        chapter_dir = self._artifacts.root / "chapters" / str(run_id)
        if replace_completed:
            chapter_dir = chapter_dir / "recompositions" / str(uuid4())
        request_snapshot = run.snapshot.get("request")
        if not isinstance(request_snapshot, dict):
            raise PipelineError(
                ErrorCode.DATABASE_INTEGRITY_FAILED,
                "chapter",
                "chapter snapshot lacks a request object",
                retryable=False,
            )
        output_spec = OutputAudioSpec.model_validate(
            cast(dict[str, object], request_snapshot).get("output_spec")
        )
        composed = compose_final(
            ordered_inputs=tuple(inputs),
            output_spec=output_spec,
            output_path=chapter_dir / "final.wav",
            timeline_path=chapter_dir / "timeline.json",
        )
        relative_path = composed.audio.path.relative_to(self._artifacts.root).as_posix()
        if replace_completed:
            return await self._chapters.publish_recomposition(
                run_id,
                final_audio=composed.audio,
                final_relative_path=relative_path,
                timeline=composed.timeline,
            )
        return await self._chapters.mark_succeeded(
            run_id,
            final_audio=composed.audio,
            final_relative_path=relative_path,
            timeline=composed.timeline,
        )
