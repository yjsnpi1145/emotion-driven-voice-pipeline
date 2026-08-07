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
