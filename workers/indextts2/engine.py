from __future__ import annotations

import math
import random
import struct
from pathlib import Path
from uuid import uuid4

from .schemas import WorkerSynthesisRequest


class RealIndexEngine:
    """Real IndexTTS2 inference shell.

    The upstream ``indextts`` package is imported lazily so GPU-free contract
    tests can inject :class:`FakeWorkerEngine` instead.
    """

    def __init__(
        self,
        model_dir: Path,
        aux_paths: dict[str, str],
        device: str = "cuda:0",
    ) -> None:
        required_aux = {"w2v_bert", "semantic_codec", "campplus", "bigvgan"}
        if set(aux_paths) != required_aux:
            raise ValueError("all four pinned auxiliary model paths are required")
        # Lazy upstream import: never auto-download from `main`.
        import numpy as np
        import torch
        from indextts.infer_v2 import IndexTTS2

        self._np = np
        self._torch = torch
        self._tts = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"),
            model_dir=str(model_dir),
            use_fp16=True,
            device=device,
            use_cuda_kernel=False,
            use_deepspeed=False,
            use_accel=False,
            use_torch_compile=False,
            aux_paths=aux_paths,
        )

    def synthesize(self, request: WorkerSynthesisRequest) -> None:
        random.seed(request.seed)
        self._np.random.seed(request.seed % (2**32))
        self._torch.manual_seed(request.seed)
        self._torch.cuda.manual_seed_all(request.seed)

        from voice_pipeline.modules.audio.atomic_output import reserve_output_path

        reservation = reserve_output_path(request.output_path)
        partial = request.output_path.with_name(
            f".{request.output_path.stem}.{uuid4()}.partial.wav"
        )
        try:
            self._tts.infer(
                spk_audio_prompt=str(request.speaker_audio_path),
                text=request.text,
                output_path=str(partial),
                emo_alpha=1.0,
                emo_vector=list(request.emotion_vector),
                use_emo_text=False,
                emo_text=None,
                use_random=False,
                verbose=False,
            )
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("IndexTTS2 did not create a non-empty WAV")
            reservation.publish(partial)
        except BaseException:
            reservation.rollback()
            raise
        finally:
            partial.unlink(missing_ok=True)


class FakeWorkerEngine:
    """Deterministic in-process fake engine for GPU-free contract tests."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self._failure = failure

    def synthesize(self, request: WorkerSynthesisRequest) -> None:
        if self._failure is not None:
            raise self._failure
        _write_simple_wav(Path(request.output_path), seconds=4.0)


def _write_simple_wav(path: Path, seconds: float, sample_rate: int = 22050) -> None:
    """Write a valid mono PCM16 WAV using only the standard library."""
    n = int(seconds * sample_rate)
    data = bytearray()
    for i in range(n):
        sample = int(0.2 * math.sin(2 * math.pi * 220 * i / sample_rate) * 32767)
        data += struct.pack("<h", sample)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(data))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(data))
    )
    path.write_bytes(header + bytes(data))
