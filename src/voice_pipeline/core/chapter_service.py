from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.gpu_queue import SerialGpuQueue
from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.core.segment_job_service import SegmentJobService
from voice_pipeline.models.chapter import ChapterRunRecord, ChapterSynthesisRequest
from voice_pipeline.models.model_profiles import ModelProfileSnapshot, ResolvedModelProfile
from voice_pipeline.models.persistence import (
    GsvModelSnapshot,
    OutputAudioSpec,
    SegmentGsvJobRequest,
    SegmentReferenceJobRequest,
)
from voice_pipeline.models.schemas import (
    EngineFingerprint,
    ExecutionContext,
    LanguageCode,
    ReferenceJobRequest,
)
from voice_pipeline.modules.audio.composer import ComposeInput, compose_final
from voice_pipeline.modules.audio.wav_probe import sha256_file
from voice_pipeline.modules.llm.director import ReferenceTextDirector
from voice_pipeline.modules.llm.models import (
    CorrectionDirection,
    DirectedSegment,
    DirectorPlan,
)
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.chapter_store import ChapterStore
from voice_pipeline.storage.job_store import SqliteJobStore
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
        corrected = await self._correct_reference_texts(plan, base_voice)
        run = await self._chapters.create_queued(
            request=request,
            director_plan=corrected,
            model_profile_snapshot=GsvModelSnapshot(
                profile=profile_snapshot,
                engine_fingerprint=self._gsv_fingerprint(),
            ).model_dump(mode="json"),
            base_voice_sha256=sha256_file(base_voice),
        )
        task = asyncio.create_task(self._run(run.run_id, base_voice, request.model_profile_id))
        self._active[run.run_id] = task
        task.add_done_callback(lambda _: self._active.pop(run.run_id, None))
        return run

    async def get(self, run_id: UUID) -> ChapterRunRecord:
        return await self._chapters.get(run_id)

    async def recover(self) -> tuple[UUID, ...]:
        return await self._chapters.mark_interrupted_running()

    async def stop(self, *, deadline: float) -> None:
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait(tasks, timeout=remaining)

    async def _correct_reference_texts(self, plan: DirectorPlan, base_voice: Path) -> DirectorPlan:
        resolver = ReferenceTextDirector(self._director)
        corrected: list[DirectedSegment] = []
        for segment in plan.segments:
            resolved = await resolver.resolve_reference_text(
                segment,
                _ServiceReferenceDurationProbe(
                    synthesis=self._synthesis,
                    queue=self._queue,
                    jobs_root=self._jobs_root / "chapter-reference-probes",
                    base_voice=base_voice,
                ),
                max_corrections=self._max_reference_corrections,
            )
            corrected.append(segment.model_copy(update={"ref_text_cn": resolved.ref_text_cn}))
        return plan.model_copy(update={"segments": tuple(corrected)})

    async def _run(self, run_id: UUID, base_voice: Path, model_profile_id: UUID) -> None:
        try:
            await self._chapters.mark_running(run_id)
            for segment in await self._chapters.list_segments(run_id):
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

    async def _compose(self, run_id: UUID) -> None:
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
        await self._chapters.mark_succeeded(
            run_id,
            final_audio=composed.audio,
            final_relative_path=relative_path,
            timeline=composed.timeline,
        )


class _ServiceReferenceDurationProbe:
    def __init__(
        self,
        *,
        synthesis: SynthesisService,
        queue: SerialGpuQueue,
        jobs_root: Path,
        base_voice: Path,
    ) -> None:
        self._synthesis = synthesis
        self._queue = queue
        self._jobs_root = jobs_root
        self._base_voice = base_voice

    async def generate_and_measure(self, text: str, vector: tuple[float, ...], seed: int) -> float:
        request_id = uuid4()
        job_id = uuid4()
        context = ExecutionContext(
            job_id=job_id,
            request_id=request_id,
            job_dir=self._jobs_root / str(job_id),
        )
        request = ReferenceJobRequest(
            request_id=request_id,
            base_voice_path=self._base_voice,
            ref_text_cn=text,
            emotion_vector=vector,  # type: ignore[arg-type]
            seed=seed,
        )
        result = await self._queue.run(lambda: self._synthesis.generate_reference(context, request))
        return result.reference.audio.duration_seconds
