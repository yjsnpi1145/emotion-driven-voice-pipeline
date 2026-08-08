from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.model_profiles import ResolvedModelProfile
from voice_pipeline.models.schemas import (
    AudioResult,
    EngineFingerprint,
    GsvSynthesisRequest,
)
from voice_pipeline.modules.audio.atomic_output import reserve_output_path
from voice_pipeline.modules.audio.wav_probe import probe_wav


def build_gsv_payload(request: GsvSynthesisRequest) -> dict[str, Any]:
    """Map the bound synthesis request to the official GPT-SoVITS /tts payload."""
    return {
        "text": request.text,
        "text_lang": request.text_lang,
        "ref_audio_path": str(request.reference.audio.path),
        "prompt_text": request.reference.ref_text_cn,
        "prompt_lang": request.prompt_lang,
        "top_k": 15,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut0",
        "batch_size": 1,
        "split_bucket": False,
        "speed_factor": request.speed_factor,
        "fragment_interval": 0.0,
        "seed": request.seed,
        "parallel_infer": False,
        "repetition_penalty": 1.35,
        "media_type": "wav",
        "streaming_mode": False,
    }


class GptSoVitsHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        expected_fingerprint: EngineFingerprint,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._expected_fingerprint = expected_fingerprint
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    def fingerprint(self) -> EngineFingerprint:
        return self._expected_fingerprint

    async def load_profile(self, profile: ResolvedModelProfile) -> None:
        await self._set_weight("/set_gpt_weights", profile.gpt_path, kind="GPT")
        await self._set_weight("/set_sovits_weights", profile.sovits_path, kind="SoVITS")

    async def _set_weight(self, endpoint: str, weights_path: Path, *, kind: str) -> None:
        try:
            response = await self._client.get(endpoint, params={"weights_path": str(weights_path)})
            if response.status_code >= 400:
                raise PipelineError(
                    ErrorCode.MODEL_SWITCH_FAILED,
                    "gsv",
                    f"GPT-SoVITS rejected {kind} weight switch",
                    retryable=True,
                    requires_engine_abort=True,
                    details={"http_status": response.status_code, "kind": kind},
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise PipelineError(
                    ErrorCode.MODEL_SWITCH_FAILED,
                    "gsv",
                    f"GPT-SoVITS returned invalid {kind} switch response",
                    retryable=True,
                    requires_engine_abort=True,
                ) from exc
            if not isinstance(payload, dict) or payload.get("message") != "success":
                raise PipelineError(
                    ErrorCode.MODEL_SWITCH_FAILED,
                    "gsv",
                    f"GPT-SoVITS did not confirm {kind} weight switch",
                    retryable=True,
                    requires_engine_abort=True,
                )
        except PipelineError:
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise PipelineError(
                ErrorCode.MODEL_SWITCH_FAILED,
                "gsv",
                f"GPT-SoVITS {kind} weight switch outcome is uncertain",
                retryable=True,
                requires_engine_abort=True,
            ) from exc

    async def synthesize(self, request: GsvSynthesisRequest, output_path: Path) -> AudioResult:
        reservation = reserve_output_path(output_path)
        partial = output_path.with_name(f".{output_path.stem}.{uuid.uuid4()}.partial.wav")
        payload = build_gsv_payload(request)
        try:
            async with self._client.stream("POST", f"{self._base_url}/tts", json=payload) as resp:
                if resp.status_code >= 400:
                    body = ""
                    try:
                        body = resp.text[:2048]
                    except Exception:
                        pass
                    raise PipelineError(
                        ErrorCode.GSV_ENGINE_ERROR,
                        "gsv",
                        f"gsv returned HTTP {resp.status_code}",
                        retryable=True,
                        requires_engine_abort=False,
                        details={"http_status": resp.status_code, "body": body},
                    )
                content_type = (resp.headers.get("content-type") or "").lower()
                if "json" in content_type or "text" in content_type:
                    raise PipelineError(
                        ErrorCode.GSV_ENGINE_ERROR,
                        "gsv",
                        "gsv returned a JSON/text response instead of audio",
                        retryable=True,
                        requires_engine_abort=False,
                        details={"content_type": content_type},
                    )
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks.extend(chunk)
            await asyncio.to_thread(self._flush_partial, partial, bytes(chunks))
            if not partial.is_file() or partial.stat().st_size == 0:
                raise PipelineError(
                    ErrorCode.INVALID_AUDIO,
                    "gsv",
                    "gsv returned an empty response body",
                    retryable=False,
                )
            # A truncated/corrupt stream fails raw decoding: outcome on the
            # remote side is uncertain, so require engine abort.
            try:
                import soundfile as sf

                sf.read(partial, dtype="float64")
            except Exception as exc:
                raise PipelineError(
                    ErrorCode.GSV_ENGINE_ERROR,
                    "gsv",
                    "gsv returned a truncated/corrupt wav stream",
                    retryable=True,
                    requires_engine_abort=True,
                    details=self._owned_details(reservation, partial),
                ) from exc
            probe_wav(partial, require_reference_window=False)
            reservation.publish(partial)
            return probe_wav(reservation.path, require_reference_window=False)
        except httpx.TimeoutException as exc:
            abort = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            self._cleanup(reservation, partial)
            raise PipelineError(
                ErrorCode.GSV_TIMEOUT,
                "gsv",
                f"gsv request timed out: {exc}",
                retryable=True,
                requires_engine_abort=abort,
                details=self._owned_details(reservation, partial),
            ) from exc
        except httpx.RequestError as exc:
            abort = not isinstance(exc, (httpx.ConnectError, httpx.PoolTimeout))
            self._cleanup(reservation, partial)
            raise PipelineError(
                ErrorCode.GSV_ENGINE_ERROR,
                "gsv",
                f"gsv http error: {exc}",
                retryable=True,
                requires_engine_abort=abort,
                details=self._owned_details(reservation, partial),
            ) from exc
        except asyncio.CancelledError:
            self._cleanup(reservation, partial)
            raise
        except PipelineError:
            self._cleanup(reservation, partial)
            raise
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _owned_details(reservation: Any, partial: Path) -> dict[str, Any]:
        return {"owned_temporary_paths": [str(partial), str(reservation.path)]}

    @staticmethod
    def _flush_partial(partial: Path, data: bytes) -> None:
        with open(partial, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _cleanup(reservation: Any, partial: Path) -> None:
        try:
            reservation.rollback()
        except Exception:
            pass
        partial.unlink(missing_ok=True)
