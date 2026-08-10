from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _tracked_files() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def test_public_repository_has_required_governance_and_release_files() -> None:
    required = {
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "MODEL_LICENSES.md",
        "PRIVACY.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/pull_request_template.md",
    }

    missing = sorted(path for path in required if not (ROOT / path).is_file())

    assert missing == []


def test_windows_launcher_wraps_the_managed_service_lifecycle() -> None:
    launcher_path = ROOT / "启动服务.bat"

    assert launcher_path.is_file()
    launcher = launcher_path.read_text(encoding="utf-8")

    for required in (
        "%~dp0",
        "pwsh",
        "scripts\\start.ps1",
        "config\\app.example.yaml",
        ".venv-control\\Scripts\\python.exe",
        "http://127.0.0.1:8765/api/v1/health",
        "VOICE_PIPELINE_NO_BROWSER",
        "VOICE_PIPELINE_NO_PAUSE",
        'start "" "http://127.0.0.1:8765/"',
    ):
        assert required in launcher

    assert "D:\\TTSsystem" not in launcher
    assert "api_v2.py" not in launcher
    assert "workers.indextts2" not in launcher


def test_windows_launcher_uses_cmd_compatible_crlf_line_endings() -> None:
    raw = (ROOT / "启动服务.bat").read_bytes()
    attributes_path = ROOT / ".gitattributes"

    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert attributes_path.is_file()
    assert "*.bat text eol=crlf" in attributes_path.read_text(encoding="utf-8")


def test_readme_documents_the_windows_double_click_launcher() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "启动服务.bat" in readme
    assert "双击" in readme


def test_tracked_tree_excludes_private_runtime_and_model_artifacts() -> None:
    forbidden_suffixes = {
        ".ckpt",
        ".db",
        ".flac",
        ".key",
        ".mp3",
        ".onnx",
        ".p12",
        ".pem",
        ".pfx",
        ".pth",
        ".safetensors",
        ".sqlite",
        ".sqlite3",
        ".wav",
    }
    tracked = _tracked_files()

    forbidden = sorted(
        path for path in tracked if Path(path).suffix.casefold() in forbidden_suffixes
    )

    assert forbidden == []
    assert not any(path.startswith(("runtime/", "models/", "external/")) for path in tracked)
    assert not any(path.casefold().endswith((".local.yaml", ".local.yml")) for path in tracked)


def test_public_scripts_and_readme_do_not_hardcode_developer_checkout() -> None:
    public_files = [
        ROOT / "README.md",
        *(ROOT / "scripts").glob("*.ps1"),
        *(ROOT / "config/env-locks").glob("*.txt"),
    ]

    offenders = [
        str(path.relative_to(ROOT))
        for path in public_files
        if "D:\\TTSsystem" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_model_download_scripts_require_explicit_license_acceptance() -> None:
    scripts = (
        ROOT / "scripts/setup-indextts.ps1",
        ROOT / "scripts/setup-gpt-sovits.ps1",
        ROOT / "scripts/setup-quality.ps1",
    )

    missing_gate = [
        str(path.relative_to(ROOT))
        for path in scripts
        if "AcceptModelLicenses" not in path.read_text(encoding="utf-8")
    ]

    assert missing_gate == []


def test_indextts_setup_requires_license_acceptance_before_using_upstream() -> None:
    script = (ROOT / "scripts/setup-indextts.ps1").read_text("utf-8")

    gate = script.index("if (-not $AcceptModelLicenses)")
    clone = script.index("git' @('-c','http.proxy='")

    assert gate < clone


def test_gpt_sovits_download_installs_pretrained_archive_into_engine_tree() -> None:
    script = (ROOT / "scripts/setup-gpt-sovits.ps1").read_text("utf-8")

    assert "Expand-Archive" in script
    assert "GPT_SoVITS\\pretrained_models" in script
    assert "$PinnedArchiveSha" in script


def test_indextts_license_is_not_mislabeled_as_mit() -> None:
    inventory = yaml.safe_load((ROOT / "config/open-source-reuse.yaml").read_text("utf-8"))
    module = next(
        item for item in inventory["modules"] if item["module_id"] == "indextts2_inference"
    )
    model_licenses = (ROOT / "MODEL_LICENSES.md").read_text("utf-8")

    assert module["candidates"][0]["spdx_license"] == ("LicenseRef-bilibili-model-use-license")
    assert "90ca4d608209584bad3a5bd5becc0b80c146e60f/LICENSE" in model_licenses
    assert "模型权重不包含在本仓库" in model_licenses


def test_package_metadata_declares_public_project_identity() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text("utf-8")

    assert 'description = "Local emotion-directed cross-language dubbing workbench"' in pyproject
    assert 'readme = "README.md"' in pyproject
    assert 'license = "Apache-2.0"' in pyproject


def test_readme_documents_current_product_and_data_boundary() -> None:
    readme = (ROOT / "README.md").read_text("utf-8")

    assert "Batches 1–2" not in readme
    assert "LLM 实时活动" in readme
    assert "自训练 GPT-SoVITS" in readme
    assert "模型权重不包含在本仓库" in readme
    assert "OpenAI 兼容 API" in readme
    assert "config/app.public.local.yaml" in readme


def test_gitignore_covers_sensitive_local_artifacts() -> None:
    ignored = (ROOT / ".gitignore").read_text("utf-8").splitlines()

    for pattern in (
        ".env",
        ".env.*",
        "*.pem",
        "*.pfx",
        "*.sqlite3",
        "*.wav",
        "*.mp3",
        "*.flac",
        "*.onnx",
        ".idea/",
        ".vscode/",
    ):
        assert pattern in ignored


def test_ci_is_windows_cpu_only_and_runs_all_public_gates() -> None:
    workflow_path = ROOT / ".github/workflows/ci.yml"
    workflow = workflow_path.read_text("utf-8")
    parsed = yaml.safe_load(workflow)

    assert parsed["permissions"] == {"contents": "read"}
    assert "windows-latest" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "uv sync --frozen --extra dev" in workflow
    assert "ruff check src tests" in workflow
    assert "mypy src workers" in workflow
    assert "node --check src/voice_pipeline/webui/app.js" in workflow
    assert "not gpu and not gpu_residency and not quality_model" in workflow
    assert "uv build --wheel" in workflow
    assert "AcceptModelLicenses" not in workflow
