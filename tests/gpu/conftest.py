"""Real-GPU golden gate fixtures.

Only active when ``VOICE_PIPELINE_RUN_GPU_TESTS=1``. When the variable is set
but a required model/config/CUDA asset is missing the fixtures FAIL loudly
with the exact missing path; they never fall back to fakes or skip.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GOLDEN_MAPPING = _REPO / "config" / "golden-assets.local.yaml"
_TEMPLATES = _REPO / "testdata" / "golden"


def _require_gpu_env() -> None:
    if os.environ.get("VOICE_PIPELINE_RUN_GPU_TESTS") != "1":
        pytest.skip("VOICE_PIPELINE_RUN_GPU_TESTS not set to 1")
    if not os.environ.get("VOICE_PIPELINE_CONFIG"):
        pytest.fail(
            "VOICE_PIPELINE_RUN_GPU_TESTS=1 but VOICE_PIPELINE_CONFIG is not set; "
            "GPU gates require the real-mode config"
        )


@pytest.fixture(scope="session")
def gpu_settings() -> Any:
    _require_gpu_env()
    config_path = Path(os.environ["VOICE_PIPELINE_CONFIG"]).resolve()
    if not config_path.is_file():
        pytest.fail(f"VOICE_PIPELINE_CONFIG missing: {config_path}")
    from voice_pipeline.core.config import load_settings

    settings = load_settings(config_path)
    if settings.mode != "real":
        pytest.fail(
            f"GPU gates require mode=real but config declares mode={settings.mode}"
        )
    return settings


@pytest.fixture(scope="session")
def golden_mapping() -> dict[str, Any]:
    _require_gpu_env()
    if not _GOLDEN_MAPPING.is_file():
        pytest.fail(
            f"golden mapping missing: {_GOLDEN_MAPPING} "
            "(user must supply config/golden-assets.local.yaml with verified assets)"
        )
    import yaml

    mapping = yaml.safe_load(_GOLDEN_MAPPING.read_text(encoding="utf-8"))
    if mapping.get("schema_version") != 1:
        pytest.fail("golden-assets.local.yaml must have schema_version: 1")
    return mapping


@pytest.fixture(scope="session")
def cuda_available() -> None:
    _require_gpu_env()
    try:
        import torch  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - only on GPU-less hosts
        pytest.fail(f"torch not importable in control env: {exc}")
    if not torch.cuda.is_available():
        pytest.fail("CUDA unavailable; GPU gates cannot run without a CUDA device")


def _asset_path(mapping: dict[str, Any], key: str) -> Path:
    asset = mapping.get("assets", {}).get(key)
    if not asset or not asset.get("base_voice_path"):
        pytest.fail(f"golden mapping asset {key!r} missing base_voice_path")
    path = Path(asset["base_voice_path"]).resolve()
    if not path.is_file():
        pytest.fail(f"golden asset file missing: {path}")
    return path


def render_golden_request(
    template_path: Path, mapping: dict[str, Any]
) -> dict[str, Any]:
    """Merge a golden template with the local verified-asset mapping."""
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if template.get("schema_version") != 1:
        pytest.fail(f"{template_path.name} must have schema_version: 1")
    case_id = template["case_id"]
    case_data = mapping.get("cases", {}).get(template["case_data_key"])
    if not case_data:
        pytest.fail(
            f"golden mapping missing case {template['case_data_key']!r} for {case_id}"
        )
    if case_data.get("target_language", case_data.get("target_text_language")) not in (
        None,
        template["target_language"],
    ):
        pytest.fail(f"case language mismatch for {case_id}")
    base_voice = _asset_path(mapping, template["asset_key"])
    emotion = case_data["emotion_vector"]
    if len(emotion) != 8 or not all(isinstance(v, (int, float)) for v in emotion):
        pytest.fail(f"case {case_id} emotion_vector must be 8 numeric values")
    if not (0.0 <= sum(emotion) <= 0.8):
        pytest.fail(f"case {case_id} emotion_vector sum must be within 0.0..0.8")
    return {
        "request_id": str(uuid.uuid4()),
        "base_voice_path": str(base_voice),
        "ref_text_cn": case_data["ref_text_cn"],
        "emotion_vector": [float(v) for v in emotion],
        "target_text": case_data["target_text"],
        "target_language": template["target_language"],
        "seed": int(case_data.get("seed", 1234)),
        "speed_factor": float(case_data.get("speed_factor", 1.0)),
    }


@pytest.fixture
def zh_ja_request(golden_mapping) -> dict[str, Any]:
    return render_golden_request(_TEMPLATES / "zh-ja-001.template.json", golden_mapping)


@pytest.fixture
def zh_en_request(golden_mapping) -> dict[str, Any]:
    return render_golden_request(_TEMPLATES / "zh-en-001.template.json", golden_mapping)


@pytest.fixture(scope="session")
def dynamic_challenge(golden_mapping) -> dict[str, Any]:
    """A random short target sentence with a random request_id (not written
    in plaintext into any golden file)."""
    _require_gpu_env()
    import random

    sentences = {
        "ja": "今日は静かな一日だった。",
        "en": "This is a quiet day.",
    }
    language = random.choice(["ja", "en"])
    return {
        "request_id": str(uuid.uuid4()),
        "base_voice_path": str(_asset_path(golden_mapping, "user_verified_primary")),
        "ref_text_cn": golden_mapping["cases"]["user_verified_zh_ja"]["ref_text_cn"],
        "emotion_vector": [0.0, 0.01, 0.1, 0.02, 0.0, 0.1, 0.0, 0.1],
        "target_text": sentences[language],
        "target_language": language,
        "seed": 20260807,
    }


@pytest.fixture(scope="session")
def run_manifest_dir(gpu_settings) -> Iterator[Path]:
    _require_gpu_env()
    target = gpu_settings.runtime_dir / "gpu-golden-runs"
    target.mkdir(parents=True, exist_ok=True)
    yield target
