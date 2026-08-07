from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import httpx

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.schemas import (
    AudioResult,
    EngineFingerprint,
    IndexSynthesisRequest,
)
from voice_pipeline.modules.audio.atomic_output import reserve_output_path
from voice_pipeline.modules.audio.wav_probe import probe_wav


class IndexTTSHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        jobs_root: Path,
        expected_fingerprint: EngineFingerprint,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._jobs_root = Path(jobs_root).resolve()
        self._expected_fingerprint = expected_fingerprint
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    def fingerprint(self) -> EngineFingerprint:
        return self._expected_fingerprint

    async def synthesize(self, request: IndexSynthesisRequest, output_path: Path) -> AudioResult:
        reservation = reserve_output_path(output_path)
        working = (output_path.parent / f".{output_path.stem}.{uuid.uuid4()}.working.wav").resolve()
        payload: dict[str, Any] = request.model_dump(mode="json")
        payload["output_path"] = str(working)
        try:
            resp = await self._client.post(f"{self._base_url}/v1/synthesize", json=payload)
        except httpx.TimeoutException as exc:
            abort = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            self._cleanup(reservation, working)
            raise PipelineError(
                ErrorCode.INDEX_TIMEOUT,
                "index",
                f"index request timed out: {exc}",
                retryable=True,
                requires_engine_abort=abort,
                details=self._owned_details(reservation, working),
            ) from exc
        except httpx.RequestError as exc:
            abort = not isinstance(exc, (httpx.ConnectError, httpx.PoolTimeout))
            self._cleanup(reservation, working)
            raise PipelineError(
                ErrorCode.INDEX_ENGINE_ERROR,
                "index",
                f"index http error: {exc}",
                retryable=True,
                requires_engine_abort=abort,
                details=self._owned_details(reservation, working),
            ) from exc
        except asyncio.CancelledError:
            self._cleanup(reservation, working)
            raise

        try:
            if resp.status_code >= 400:
                raise PipelineError(
                    ErrorCode.INDEX_ENGINE_ERROR,
                    "index",
                    f"index returned HTTP {resp.status_code}",
                    retryable=True,
                    requires_engine_abort=False,
                    details={
                        "http_status": resp.status_code,
                        "body": resp.text[:2048],
                    },
                )
            try:
                body: dict[str, Any] = resp.json()
            except Exception as exc:
                raise PipelineError(
                    ErrorCode.INDEX_ENGINE_ERROR,
                    "index",
                    "index returned truncated/invalid JSON body",
                    retryable=True,
                    requires_engine_abort=True,
                    details=self._owned_details(reservation, working),
                ) from exc
            self._verify_response(request, body, working)
            if not working.is_file() or working.stat().st_size == 0:
                raise PipelineError(
                    ErrorCode.INVALID_AUDIO,
                    "index",
                    "worker did not create a non-empty output wav",
                    retryable=False,
                )
            result = probe_wav(working, require_reference_window=False)
            reservation.publish(working)
            return result.model_copy(update={"path": reservation.path})
        except PipelineError:
            self._cleanup(reservation, working)
            raise
        finally:
            working.unlink(missing_ok=True)

    def _verify_response(
        self,
        request: IndexSynthesisRequest,
        body: dict[str, Any],
        working: Path,
    ) -> None:
        if str(body.get("request_id")) != str(request.request_id):
            raise PipelineError(
                ErrorCode.INDEX_ENGINE_ERROR,
                "index",
                "worker request_id mismatch",
                retryable=False,
                requires_engine_abort=False,
            )
        effective = body.get("effective_emotion_vector")
        if effective != list(request.emotion_vector):
            raise PipelineError(
                ErrorCode.INDEX_ENGINE_ERROR,
                "index",
                "worker effective emotion vector mismatch",
                retryable=False,
                requires_engine_abort=False,
            )
        fingerprint = body.get("engine_fingerprint")
        if fingerprint != self._expected_fingerprint.model_dump(mode="json"):
            raise PipelineError(
                ErrorCode.INDEX_ENGINE_ERROR,
                "index",
                "worker engine fingerprint mismatch",
                retryable=False,
                requires_engine_abort=False,
            )
        returned_path = body.get("output_path")
        if not returned_path or Path(str(returned_path)).resolve() != working:
            raise PipelineError(
                ErrorCode.INDEX_ENGINE_ERROR,
                "index",
                "worker output path mismatch",
                retryable=False,
                requires_engine_abort=False,
            )

    @staticmethod
    def _owned_details(reservation: Any, working: Path) -> dict[str, Any]:
        return {"owned_temporary_paths": [str(working), str(reservation.path)]}

    @staticmethod
    def _cleanup(reservation: Any, working: Path) -> None:
        try:
            reservation.rollback()
        except Exception:
            pass
        working.unlink(missing_ok=True)
