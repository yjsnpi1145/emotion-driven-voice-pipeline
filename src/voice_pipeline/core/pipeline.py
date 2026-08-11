from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psutil

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.core.inference_tracker import (
    InferenceTracker,
    TrackerLease,
    fake_fingerprint,
)
from voice_pipeline.models.model_profiles import ResolvedModelProfile
from voice_pipeline.models.persistence import OutputAudioSpec
from voice_pipeline.models.schemas import (
    EngineFingerprint,
    EngineIdentity,
    ExecutionContext,
    GsvJobRequest,
    GsvSynthesisRequest,
    GsvSynthesisResult,
    IndexSynthesisRequest,
    ReferenceBinding,
    ReferenceJobRequest,
    ReferenceSynthesisResult,
    RuntimeHealth,
    SegmentSynthesisRequest,
    SegmentSynthesisResult,
    WorkerHealth,
    WorkerName,
    WorkersHealth,
)
from voice_pipeline.modules.audio.atomic_output import atomic_write_json
from voice_pipeline.modules.audio.wav_probe import probe_wav, sha256_file
from voice_pipeline.modules.cache.keys import (
    build_gsv_cache_key,
    build_quality_cache_key,
    build_reference_cache_key,
)
from voice_pipeline.modules.quality.models import QualityReport
from voice_pipeline.modules.quality.ports import QualityAnalyzer, SavedQualityReportValidator
from voice_pipeline.storage.artifact_store import ArtifactStore
from voice_pipeline.storage.cache_store import CacheStore
from voice_pipeline.storage.quality_cache import QualityCacheStore


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_reference_manifest(
    context: ExecutionContext,
    request: ReferenceJobRequest | SegmentSynthesisRequest,
    binding: ReferenceBinding,
    quality_result: QualityReport | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "manifest_type": "reference",
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "request_snapshot": request.model_dump(mode="json"),
        "seed": request.seed,
        "effective_inference_parameters": {
            "use_random": False,
            "require_reference_window": True,
        },
        "engine_and_checkpoint_fingerprints": binding.engine_fingerprint.model_dump(mode="json"),
        "output_audio_metrics_and_sha256": binding.audio.model_dump(mode="json"),
        "reference": binding.model_dump(mode="json"),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    if quality_result is not None:
        result["quality_result"] = quality_result.model_dump(mode="json")
    return result


def build_run_manifest(
    context: ExecutionContext,
    request: SegmentSynthesisRequest,
    result: SegmentSynthesisResult,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest_type": "run",
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "request_snapshot": request.model_dump(mode="json"),
        "seed": request.seed,
        "effective_inference_parameters": {
            "speed_factor": request.speed_factor,
            "target_language": request.target_language,
        },
        "engine_and_checkpoint_fingerprints": (
            result.reference_binding.engine_fingerprint.model_dump(mode="json")
        ),
        "output_audio_metrics_and_sha256": {
            "reference": result.reference.model_dump(mode="json"),
            "target": result.target.model_dump(mode="json"),
        },
        "reference_manifest_path": str(result.reference_manifest_path),
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


def build_gsv_run_manifest(
    context: ExecutionContext,
    request: GsvJobRequest,
    binding: ReferenceBinding,
    target_audio: Any,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest_type": "run",
        "job_id": str(context.job_id),
        "request_id": str(context.request_id),
        "request_snapshot": request.model_dump(mode="json"),
        "seed": request.seed,
        "effective_inference_parameters": {
            "speed_factor": request.speed_factor,
            "target_language": request.target_language,
        },
        "engine_and_checkpoint_fingerprints": binding.engine_fingerprint.model_dump(mode="json"),
        "output_audio_metrics_and_sha256": {"target": target_audio.model_dump(mode="json")},
        "reference_content_sha256": binding.audio.content_sha256,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }


class NoopEngineRuntime:
    """In-process fake-mode runtime: no subprocesses, tracker-backed leases."""

    def __init__(self) -> None:
        self._tracker = InferenceTracker()
        self._fingerprints: dict[WorkerName, EngineFingerprint] = {
            "indextts": fake_fingerprint("indextts"),
            "gpt_sovits": fake_fingerprint("gpt_sovits"),
        }
        self._state: dict[WorkerName, str] = {
            "indextts": "ready",
            "gpt_sovits": "ready",
        }

    async def start(self) -> None:
        return None

    async def stop(self, *, deadline: float | None = None) -> None:
        return None

    async def ensure_engine(self, engine: WorkerName) -> None:
        self._require_known(engine)
        self._state[engine] = "ready"

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None:
        self._require_known(engine)
        # The lease confirm_aborted() zeroes active inference; nothing to kill.

    def engine_identity(self, engine: WorkerName) -> EngineIdentity:
        self._require_known(engine)
        if self._state[engine] != "ready" or self._tracker.is_unknown(engine):
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                f"{engine} is not ready",
                retryable=False,
            )
        return EngineIdentity(
            worker=engine,
            pid=os.getpid(),
            create_time=psutil.Process().create_time(),
            python_executable=Path(sys.executable),
            fingerprint=self._fingerprints[engine],
        )

    def health(self) -> RuntimeHealth:
        workers: dict[str, WorkerHealth] = {}
        for engine in ("indextts", "gpt_sovits"):
            state = "unknown" if self._tracker.is_unknown(engine) else self._state[engine]
            fingerprint = self._fingerprints[engine]
            workers[engine] = WorkerHealth(
                state=state,  # type: ignore[arg-type]
                pid=os.getpid(),
                create_time=psutil.Process().create_time(),
                python_executable=Path(sys.executable),
                python_version=sys.version.split()[0],
                source_revision="in-process-fake",
                fingerprint=fingerprint,
                preflight_ok=True,
                active_inference=self._tracker.active_count(engine),
            )
        degraded = any(worker.state in ("unknown", "unhealthy") for worker in workers.values())
        return RuntimeHealth(
            status="degraded" if degraded else "ready",
            workers=WorkersHealth(indextts=workers["indextts"], gpt_sovits=workers["gpt_sovits"]),
        )

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> TrackerLease:
        self._require_known(engine)
        return await self._tracker.begin(engine, job_id=job_id)

    @staticmethod
    def _require_known(engine: WorkerName) -> None:
        if engine not in ("indextts", "gpt_sovits"):
            raise ValueError(f"unknown engine: {engine}")


class SynthesisService:
    def __init__(self, *, index: Any, gsv: Any, runtime: Any, audit: Any) -> None:
        self._index = index
        self._gsv = gsv
        self._runtime = runtime
        self._audit = audit
        self._model_profile_resolver: (
            Callable[[UUID | None], Awaitable[ResolvedModelProfile]] | None
        ) = None
        self._require_model_profile = False
        self._cache: CacheStore | None = None
        self._artifact_store: ArtifactStore | None = None
        self._quality_analyzer: QualityAnalyzer | None = None
        self._quality_cache: QualityCacheStore | None = None

    def configure_cache(self, cache: CacheStore, artifact_store: ArtifactStore) -> None:
        self._cache = cache
        self._artifact_store = artifact_store

    def configure_quality(self, analyzer: QualityAnalyzer) -> None:
        self._quality_analyzer = analyzer

    @property
    def quality_analyzer(self) -> QualityAnalyzer | None:
        return self._quality_analyzer

    def configure_quality_cache(self, cache: QualityCacheStore) -> None:
        self._quality_cache = cache

    async def _check_reference_quality(
        self, *, audio_path: Path, expected_text: str
    ) -> QualityReport | None:
        analyzer = self._quality_analyzer
        if analyzer is None:
            return None
        audio_sha256 = sha256_file(audio_path)
        key = build_quality_cache_key(
            audio_sha256=audio_sha256,
            expected_text=expected_text,
            policy_fingerprint=analyzer.policy_fingerprint,
        )
        report = await self._quality_cache.get_valid(key) if self._quality_cache else None
        if report is not None and report.policy_fingerprint != analyzer.policy_fingerprint:
            report = None
        if report is None:
            report = await analyzer.analyze_reference(
                audio_path=audio_path,
                expected_text=expected_text,
            )
            if self._quality_cache is not None:
                if report.policy_fingerprint != key.payload["policy_fingerprint"]:
                    key = build_quality_cache_key(
                        audio_sha256=audio_sha256,
                        expected_text=expected_text,
                        policy_fingerprint=report.policy_fingerprint,
                    )
                await self._quality_cache.put(key, report)
        if report.passed:
            return report
        code = (
            ErrorCode.QUALITY_VAD_FAILED
            if report.failure_code == "QUALITY_VAD_FAILED"
            else ErrorCode.QUALITY_TEXT_MISMATCH
        )
        raise PipelineError(
            code,
            "quality",
            "reference audio did not satisfy the configured quality policy",
            retryable=False,
            details={"quality_result": report.model_dump(mode="json")},
        )

    def _require_saved_reference_quality(self, manifest: dict[str, Any]) -> QualityReport | None:
        analyzer = self._quality_analyzer
        if analyzer is None:
            return None
        try:
            report = QualityReport.model_validate(manifest["quality_result"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(
                ErrorCode.QUALITY_TEXT_MISMATCH,
                "quality",
                "reference manifest does not contain a valid quality report",
                retryable=False,
            ) from exc
        report_is_valid = (
            analyzer.accepts_saved_report(report)
            if isinstance(analyzer, SavedQualityReportValidator)
            else report.passed and report.policy_fingerprint == analyzer.policy_fingerprint
        )
        if not report_is_valid:
            code = (
                ErrorCode.QUALITY_VAD_FAILED
                if report.failure_code == "QUALITY_VAD_FAILED"
                else ErrorCode.QUALITY_TEXT_MISMATCH
            )
            raise PipelineError(
                code,
                "quality",
                "reference manifest quality report is not valid for this policy",
                retryable=False,
            )
        return report

    async def _cache_hit(self, key: Any, destination: Path, *, reference: bool) -> Any | None:
        if self._cache is None or self._artifact_store is None:
            return None
        hit = await self._cache.get_valid(key)
        if hit is None:
            return None
        copied = self._artifact_store.materialize_job_output(hit.blob, destination)
        return probe_wav(copied.path, require_reference_window=reference)

    async def _cache_put(self, key: Any, audio_path: Path) -> None:
        if self._cache is None or self._artifact_store is None:
            return
        blob = self._artifact_store.publish_blob(
            self._artifact_store.stage_audio(uuid4(), audio_path)
        )
        await self._cache.put(key, blob)

    def configure_model_profile_resolver(
        self,
        resolver: Callable[[UUID | None], Awaitable[ResolvedModelProfile]],
        *,
        require_model_profile: bool,
    ) -> None:
        self._model_profile_resolver = resolver
        self._require_model_profile = require_model_profile

    async def _resolve_model_profile(self, profile_id: UUID | None) -> ResolvedModelProfile | None:
        if self._model_profile_resolver is None:
            return None
        try:
            return await self._model_profile_resolver(profile_id)
        except PipelineError as exc:
            if (
                not self._require_model_profile
                and profile_id is None
                and exc.code == ErrorCode.MODEL_PROFILE_UNAVAILABLE
                and exc.details.get("reason") == "no_active_profile"
            ):
                return None
            raise

    async def _load_and_synthesize_gsv(
        self,
        model_profile: ResolvedModelProfile | None,
        request: GsvSynthesisRequest,
        output_path: Path,
    ) -> Any:
        # Weight loading and TTS share one runtime lease.  A failed/uncertain
        # switch therefore triggers the same abort-and-poison rules as a failed
        # synthesis rather than allowing a following job onto an unknown model.
        if model_profile is not None:
            await self._gsv.load_profile(model_profile)
        return await self._gsv.synthesize(request, output_path)

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def _validate_context(self, context: ExecutionContext, request_id: UUID) -> None:
        if context.request_id != request_id:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                "context request_id does not match request request_id",
                retryable=False,
            )

    def _validate_input_path(self, path: Path) -> None:
        if not path.is_absolute():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                f"path must be absolute: {path}",
                retryable=False,
            )
        if path.is_symlink():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                f"path must not be a symlink: {path}",
                retryable=False,
            )
        if not path.is_file():
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                f"path must be an existing regular file: {path}",
                retryable=False,
            )

    def _validate_inputs(self, request: object) -> None:
        if isinstance(request, (SegmentSynthesisRequest, ReferenceJobRequest)):
            self._validate_input_path(request.base_voice_path)
        elif isinstance(request, GsvJobRequest):
            self._validate_input_path(request.reference_manifest_path)
        else:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                "unsupported request type",
                retryable=False,
            )

    # ------------------------------------------------------------------ #
    # engine invocation
    # ------------------------------------------------------------------ #

    async def _invoke_engine(
        self,
        engine: WorkerName,
        factory: Callable[[], Awaitable[Any]],
        *,
        job_id: UUID,
    ) -> Any:
        lease = await self._runtime.begin_inference(engine, job_id=job_id)
        try:
            result = await factory()
        except PipelineError as exc:
            if exc.requires_engine_abort:
                await self._abort_or_mark_unknown(engine, lease)
            else:
                await lease.confirm_completed()
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._abort_or_mark_unknown(engine, lease))
            raise
        else:
            await lease.confirm_completed()
            return result

    async def _abort_or_mark_unknown(self, engine: WorkerName, lease: Any) -> None:
        try:
            await self._runtime.abort_engine(engine, reason="engine inference outcome uncertain")
        except Exception as exc:
            try:
                await lease.mark_unknown()
            except Exception:
                pass
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "runtime",
                "abort could not be confirmed",
                retryable=False,
                poison_queue=True,
            ) from exc
        await lease.confirm_aborted()

    # ------------------------------------------------------------------ #
    # audit
    # ------------------------------------------------------------------ #

    def _write_audit(
        self,
        *,
        job_id: UUID,
        request_id: UUID,
        engine: WorkerName,
        event: str,
        identity: EngineIdentity,
        target_text_sha256_or_null: str | None = None,
        reference_sha256_or_null: str | None = None,
        fingerprint: EngineFingerprint | None = None,
    ) -> None:
        try:
            self._audit.write(
                job_id=job_id,
                request_id=request_id,
                engine=engine,
                event=event,
                engine_pid=identity.pid,
                engine_create_time=identity.create_time,
                target_text_sha256_or_null=target_text_sha256_or_null,
                reference_sha256_or_null=reference_sha256_or_null,
                engine_fingerprint=fingerprint,
            )
        except Exception as exc:
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "audit",
                "audit write failed; refusing to continue inference",
                retryable=False,
            ) from exc

    # ------------------------------------------------------------------ #
    # service methods
    # ------------------------------------------------------------------ #

    async def generate_reference(
        self,
        context: ExecutionContext,
        request: ReferenceJobRequest,
        *,
        enforce_reference_window: bool = True,
    ) -> ReferenceSynthesisResult:
        self._validate_context(context, request.request_id)
        self._validate_inputs(request)
        job_dir = context.job_dir
        job_dir.mkdir(parents=True, exist_ok=False)
        reference_path = job_dir / "reference.wav"

        index_request = IndexSynthesisRequest(
            request_id=request.request_id,
            text=request.ref_text_cn,
            speaker_audio_path=request.base_voice_path,
            emotion_vector=request.emotion_vector,
            seed=request.seed,
            use_random=False,
        )
        reference_key = build_reference_cache_key(
            index_request,
            base_voice_sha256=sha256_file(request.base_voice_path),
            engine_fingerprint=self._index.fingerprint(),
            output_spec=OutputAudioSpec(sample_rate=22050),
        )
        reference_audio = (
            await self._cache_hit(
                reference_key,
                reference_path,
                reference=enforce_reference_window,
            )
            if request.seed >= 0
            else None
        )
        reference_cache_hit = reference_audio is not None
        if reference_audio is None:
            await self._runtime.ensure_engine("indextts")
            identity = self._runtime.engine_identity("indextts")
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="indextts",
                event="inference_started",
                identity=identity,
                target_text_sha256_or_null=_sha256_hex(request.ref_text_cn),
                fingerprint=self._index.fingerprint(),
            )
            reference_audio = await self._invoke_engine(
                "indextts",
                lambda: self._index.synthesize(index_request, reference_path),
                job_id=context.job_id,
            )
            reference_audio = probe_wav(
                reference_audio.path,
                require_reference_window=enforce_reference_window,
            )
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="indextts",
                event="inference_completed",
                identity=identity,
                target_text_sha256_or_null=_sha256_hex(request.ref_text_cn),
                reference_sha256_or_null=reference_audio.content_sha256,
                fingerprint=self._index.fingerprint(),
            )
        quality_result = (
            await self._check_reference_quality(
                audio_path=reference_audio.path,
                expected_text=request.ref_text_cn,
            )
            if enforce_reference_window
            else None
        )
        if request.seed >= 0 and not reference_cache_hit:
            await self._cache_put(reference_key, reference_audio.path)
        binding = ReferenceBinding(
            audio=reference_audio,
            ref_text_cn=request.ref_text_cn,
            emotion_vector=request.emotion_vector,
            base_voice_sha256=sha256_file(request.base_voice_path),
            engine_fingerprint=self._index.fingerprint(),
        )
        reference_manifest_path = job_dir / "reference-manifest.json"
        atomic_write_json(
            reference_manifest_path,
            build_reference_manifest(context, request, binding, quality_result),
        )
        return ReferenceSynthesisResult(
            job_id=context.job_id,
            request_id=context.request_id,
            reference=binding,
            manifest_path=reference_manifest_path,
        )

    async def generate_gsv(
        self, context: ExecutionContext, request: GsvJobRequest
    ) -> GsvSynthesisResult:
        self._validate_context(context, request.request_id)
        self._validate_inputs(request)
        manifest_path = request.reference_manifest_path
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        binding = ReferenceBinding.model_validate(manifest_raw["reference"])
        self._require_saved_reference_quality(manifest_raw)
        self._validate_input_path(binding.audio.path)
        reference_audio = probe_wav(binding.audio.path, require_reference_window=True)
        if reference_audio.content_sha256 != binding.audio.content_sha256:
            raise PipelineError(
                ErrorCode.INVALID_INPUT,
                "input",
                "reference wav sha256 does not match its manifest",
                retryable=False,
            )
        before_reference_sha = sha256_file(binding.audio.path)
        before_manifest_sha = sha256_file(manifest_path)

        job_dir = context.job_dir
        job_dir.mkdir(parents=True, exist_ok=False)
        target_path = job_dir / "target.wav"

        gsv_request = GsvSynthesisRequest(
            request_id=request.request_id,
            reference=binding,
            text=request.target_text,
            text_lang=request.target_language,
            speed_factor=request.speed_factor,
            seed=request.seed,
            model_profile_id=request.model_profile_id,
        )
        await self._runtime.ensure_engine("gpt_sovits")
        model_profile = await self._resolve_model_profile(request.model_profile_id)
        identity = self._runtime.engine_identity("gpt_sovits")
        self._write_audit(
            job_id=context.job_id,
            request_id=context.request_id,
            engine="gpt_sovits",
            event="inference_started",
            identity=identity,
            target_text_sha256_or_null=_sha256_hex(request.target_text),
            reference_sha256_or_null=binding.audio.content_sha256,
            fingerprint=self._gsv.fingerprint(),
        )
        target_audio = await self._invoke_engine(
            "gpt_sovits",
            lambda: self._load_and_synthesize_gsv(model_profile, gsv_request, target_path),
            job_id=context.job_id,
        )
        target_audio = probe_wav(target_audio.path, require_reference_window=False)
        self._write_audit(
            job_id=context.job_id,
            request_id=context.request_id,
            engine="gpt_sovits",
            event="inference_completed",
            identity=identity,
            target_text_sha256_or_null=_sha256_hex(request.target_text),
            reference_sha256_or_null=binding.audio.content_sha256,
            fingerprint=self._gsv.fingerprint(),
        )

        if sha256_file(binding.audio.path) != before_reference_sha:
            raise PipelineError(
                ErrorCode.GSV_ENGINE_ERROR,
                "gsv",
                "reference wav changed during gsv generation",
                retryable=False,
            )
        if sha256_file(manifest_path) != before_manifest_sha:
            raise PipelineError(
                ErrorCode.GSV_ENGINE_ERROR,
                "gsv",
                "reference manifest changed during gsv generation",
                retryable=False,
            )

        run_manifest_path = job_dir / "run-manifest.json"
        atomic_write_json(
            run_manifest_path,
            build_gsv_run_manifest(context, request, binding, target_audio),
        )
        return GsvSynthesisResult(
            job_id=context.job_id,
            request_id=context.request_id,
            target=target_audio,
            reference_content_sha256=binding.audio.content_sha256,
            manifest_path=run_manifest_path,
        )

    async def synthesize_segment(
        self,
        context: ExecutionContext,
        request: SegmentSynthesisRequest,
    ) -> SegmentSynthesisResult:
        self._validate_context(context, request.request_id)
        self._validate_inputs(request)
        job_dir = context.job_dir
        job_dir.mkdir(parents=True, exist_ok=False)
        reference_path = job_dir / "reference.wav"
        target_path = job_dir / "target.wav"

        index_request = IndexSynthesisRequest(
            request_id=request.request_id,
            text=request.ref_text_cn,
            speaker_audio_path=request.base_voice_path,
            emotion_vector=request.emotion_vector,
            seed=request.seed,
            use_random=False,
        )
        reference_key = build_reference_cache_key(
            index_request,
            base_voice_sha256=sha256_file(request.base_voice_path),
            engine_fingerprint=self._index.fingerprint(),
            output_spec=OutputAudioSpec(sample_rate=22050),
        )
        reference_audio = (
            await self._cache_hit(reference_key, reference_path, reference=True)
            if request.seed >= 0
            else None
        )
        if reference_audio is None:
            await self._runtime.ensure_engine("indextts")
            identity = self._runtime.engine_identity("indextts")
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="indextts",
                event="inference_started",
                identity=identity,
                target_text_sha256_or_null=_sha256_hex(request.ref_text_cn),
                fingerprint=self._index.fingerprint(),
            )
            reference_audio = await self._invoke_engine(
                "indextts",
                lambda: self._index.synthesize(index_request, reference_path),
                job_id=context.job_id,
            )
            reference_audio = probe_wav(reference_audio.path, require_reference_window=True)
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="indextts",
                event="inference_completed",
                identity=identity,
                target_text_sha256_or_null=_sha256_hex(request.ref_text_cn),
                reference_sha256_or_null=reference_audio.content_sha256,
                fingerprint=self._index.fingerprint(),
            )
        quality_result = await self._check_reference_quality(
            audio_path=reference_audio.path,
            expected_text=request.ref_text_cn,
        )
        if request.seed >= 0:
            await self._cache_put(reference_key, reference_audio.path)
        binding = ReferenceBinding(
            audio=reference_audio,
            ref_text_cn=request.ref_text_cn,
            emotion_vector=request.emotion_vector,
            base_voice_sha256=sha256_file(request.base_voice_path),
            engine_fingerprint=self._index.fingerprint(),
        )
        reference_manifest_path = job_dir / "reference-manifest.json"
        atomic_write_json(
            reference_manifest_path,
            build_reference_manifest(context, request, binding, quality_result),
        )

        gsv_request = GsvSynthesisRequest(
            request_id=request.request_id,
            reference=binding,
            text=request.target_text,
            text_lang=request.target_language,
            speed_factor=request.speed_factor,
            seed=request.seed,
            model_profile_id=request.model_profile_id,
        )
        gsv_key = build_gsv_cache_key(
            gsv_request,
            engine_fingerprint=self._gsv.fingerprint(),
            output_spec=OutputAudioSpec(sample_rate=32000),
        )
        target_audio = (
            await self._cache_hit(gsv_key, target_path, reference=False)
            if request.seed >= 0
            else None
        )
        if target_audio is None:
            await self._runtime.ensure_engine("gpt_sovits")
            model_profile = await self._resolve_model_profile(request.model_profile_id)
            gsv_identity = self._runtime.engine_identity("gpt_sovits")
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="gpt_sovits",
                event="inference_started",
                identity=gsv_identity,
                target_text_sha256_or_null=_sha256_hex(request.target_text),
                reference_sha256_or_null=binding.audio.content_sha256,
                fingerprint=self._gsv.fingerprint(),
            )
            target_audio = await self._invoke_engine(
                "gpt_sovits",
                lambda: self._load_and_synthesize_gsv(model_profile, gsv_request, target_path),
                job_id=context.job_id,
            )
            target_audio = probe_wav(target_audio.path, require_reference_window=False)
            self._write_audit(
                job_id=context.job_id,
                request_id=context.request_id,
                engine="gpt_sovits",
                event="inference_completed",
                identity=gsv_identity,
                target_text_sha256_or_null=_sha256_hex(request.target_text),
                reference_sha256_or_null=binding.audio.content_sha256,
                fingerprint=self._gsv.fingerprint(),
            )
            if request.seed >= 0:
                await self._cache_put(gsv_key, target_audio.path)
        result = SegmentSynthesisResult(
            job_id=context.job_id,
            request_id=context.request_id,
            reference=reference_audio,
            target=target_audio,
            reference_binding=binding,
            reference_manifest_path=reference_manifest_path,
            run_manifest_path=job_dir / "run-manifest.json",
        )
        atomic_write_json(
            result.run_manifest_path,
            build_run_manifest(context, request, result),
        )
        return result
