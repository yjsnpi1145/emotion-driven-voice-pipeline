from __future__ import annotations

import tomllib
from pathlib import Path

from voice_pipeline.models.delivery import Batch1AcceptanceReceipt

ROOT = Path(__file__).resolve().parents[2]


def test_batch1_handoff_allows_explicit_user_golden_waiver() -> None:
    receipt = Batch1AcceptanceReceipt.model_validate(
        {
            "schema_version": 1,
            "commit_sha": "a" * 40,
            "engineering_disposition": "PASS",
            "golden_listening": "waived_by_user",
            "waiver_reason": "user explicitly requested to skip golden acceptance",
        }
    )

    assert receipt.engineering_disposition == "PASS"
    assert receipt.golden_listening == "waived_by_user"


def test_pytest_collection_is_limited_to_project_test_tree() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_control_setup_does_not_seed_packages_excluded_from_runtime_lock() -> None:
    script = (ROOT / "scripts" / "setup-control.ps1").read_text(encoding="utf-8")

    assert "'--seed'" not in script


def test_engine_setup_allows_ignored_local_model_assets_on_repeat_runs() -> None:
    script = (ROOT / "scripts" / "setup-indextts.ps1").read_text(encoding="utf-8")

    assert "checkpoints" in script
    assert "status --porcelain" in script


def test_index_setup_uses_the_cuda_index_required_by_its_locked_torch_wheel() -> None:
    script = (ROOT / "scripts" / "setup-indextts.ps1").read_text(encoding="utf-8")

    assert "'--extra-index-url','https://download.pytorch.org/whl/cu128'" in script


def test_index_setup_repairs_an_existing_incomplete_venv() -> None:
    script = (ROOT / "scripts" / "setup-indextts.ps1").read_text(encoding="utf-8")

    assert "$NeedsSync" in script
    assert "if ($NeedsSync)" in script


def test_index_setup_uses_the_lock_compilation_index_strategy() -> None:
    script = (ROOT / "scripts" / "setup-indextts.ps1").read_text(encoding="utf-8")

    assert "'--index-strategy','unsafe-best-match'" in script


def test_index_setup_installs_the_pinned_upstream_package_into_its_worker_venv() -> None:
    script = (ROOT / "scripts" / "setup-indextts.ps1").read_text(encoding="utf-8")

    assert "'--no-deps', '--editable', $IndexRepo" in script


def test_gsv_setup_reconciles_existing_conda_env_with_the_cuda_lock() -> None:
    script = (ROOT / "scripts" / "setup-gpt-sovits.ps1").read_text(encoding="utf-8")

    assert "$NeedsPipSync" in script
    assert "'--extra-index-url','https://download.pytorch.org/whl/cu128'" in script
    assert "'--index-strategy','unsafe-best-match'" in script


def test_gsv_conda_lock_is_a_valid_explicit_conda_specification() -> None:
    lock = (ROOT / "config" / "env-locks" / "gsv-conda-explicit.txt").read_text(encoding="utf-8")

    assert "@EXPLICIT" in lock


def test_gsv_setup_allows_its_owned_untracked_conda_environment_on_repeat_runs() -> None:
    script = (ROOT / "scripts" / "setup-gpt-sovits.ps1").read_text(encoding="utf-8")

    assert "$Dirty = git -C $GsvRepo status --porcelain |" in script
    assert "Where-Object" in script


def test_checkpoint_lock_script_uses_builtin_json_compatible_yaml_serialization() -> None:
    script = (ROOT / "scripts" / "lock-engine-assets.ps1").read_text(encoding="utf-8")

    assert "ConvertTo-Yaml" not in script
    assert "ConvertFrom-Yaml" not in script
    assert "ConvertTo-Json -Depth" in script
    assert "ConvertFrom-Json" in script


def test_real_gsv_worker_receives_project_local_nltk_data_path() -> None:
    source = (ROOT / "src" / "voice_pipeline" / "runtime" / "process.py").read_text(
        encoding="utf-8"
    )

    assert '"NLTK_DATA"' in source
    assert '"runtime" / "nltk_data"' in source


def test_checkpoint_lock_covers_project_local_gsv_nltk_assets() -> None:
    script = (ROOT / "scripts" / "lock-engine-assets.ps1").read_text(encoding="utf-8")

    assert "$GsvNltkData" in script


def test_real_worker_identity_uses_the_worker_interpreter_not_control_python() -> None:
    source = (ROOT / "src" / "voice_pipeline" / "runtime" / "process.py").read_text(
        encoding="utf-8"
    )

    assert "python_executable=python" in source
