from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
