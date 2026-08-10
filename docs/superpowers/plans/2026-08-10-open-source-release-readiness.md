# Open Source Release Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a source-only, license-correct, path-portable and CI-verified public GitHub repository for the local Windows TTS workbench.

**Architecture:** Keep all model engines external and pinned while licensing only this repository's original code under Apache-2.0. Add repository-policy tests that inspect tracked files and public scripts, then make setup paths portable, rewrite public documentation, and validate the result from a clean tracked-tree copy.

**Tech Stack:** Python 3.11, PowerShell 7, uv, pytest, Ruff, Mypy, FastAPI, GitHub Actions.

## Global Constraints

- Do not track model weights, trained voices, audio outputs, SQLite state, API keys or `.local.yaml` configuration.
- Project code is Apache-2.0; IndexTTS2 remains under its fixed upstream bilibili Model Use License Agreement.
- Public setup must work from any Windows path and must derive the repository root from `$PSScriptRoot`.
- CI uses fake/CPU paths only and never downloads models.
- Preserve the existing real local deployment and ignored runtime state.

---

### Task 1: Repository policy tests

**Files:**
- Create: `tests/repository/test_public_release_policy.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `git ls-files`, public PowerShell scripts and root governance files.
- Produces: pytest checks for required files, forbidden tracked suffixes, hard-coded developer paths and upstream license labels.

- [ ] Write tests asserting `LICENSE`, `MODEL_LICENSES.md`, `PRIVACY.md`, `SECURITY.md`, CI workflow and portable scripts.
- [ ] Run `pytest -q tests/repository/test_public_release_policy.py` and confirm it fails on the missing files and `D:\TTSsystem` script roots.
- [ ] Add the `repository` pytest marker and only the minimal policy helpers needed by the assertions.
- [ ] Re-run the repository tests after each later task.

### Task 2: License and model boundary

**Files:**
- Create: `LICENSE`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `MODEL_LICENSES.md`
- Modify: `config/open-source-reuse.yaml`
- Modify: `docs/batch-1-open-source-reuse.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: pinned upstream revisions in `config/engines.lock.yaml`.
- Produces: machine-readable project license metadata and human-readable separate model terms.

- [ ] Add the unmodified Apache License 2.0 text.
- [ ] Declare `license = "Apache-2.0"`, README, description, keywords and classifiers in package metadata; add project URLs only after the canonical GitHub owner/repository exists.
- [ ] Replace the false IndexTTS2 MIT labels with `LicenseRef-bilibili-model-use-license` and the pinned license URL.
- [ ] Document that weights are downloaded separately and are never relicensed by this repository.
- [ ] Run repository policy tests and `uv lock --check`.

### Task 3: Portable setup and safe download consent

**Files:**
- Modify: `scripts/setup-control.ps1`
- Modify: `scripts/setup-indextts.ps1`
- Modify: `scripts/setup-gpt-sovits.ps1`
- Modify: `scripts/setup-quality.ps1`
- Modify: `scripts/probe-engine-lifecycle.ps1`
- Modify: `scripts/lock-engine-assets.ps1`
- Modify: `tests/repository/test_public_release_policy.py`

**Interfaces:**
- Consumes: each script's own `$PSScriptRoot`.
- Produces: a resolved repository root and explicit `-AcceptModelLicenses` gates for scripts that download model assets.

- [ ] Extend the failing policy tests to inspect every tracked `scripts/*.ps1` file for `D:\TTSsystem` and model download consent.
- [ ] Verify the new assertions fail.
- [ ] Derive `$RepoRoot` using `Resolve-Path (Join-Path $PSScriptRoot '..')` and update help examples to relative paths.
- [ ] Add mandatory `-AcceptModelLicenses` to IndexTTS2, GPT-SoVITS and quality-model download paths while allowing already-installed offline verification without a new download.
- [ ] Run repository policy and existing script/process tests.

### Task 4: Public documentation and repository hygiene

**Files:**
- Rewrite: `README.md`
- Create: `docs/installation-windows.md`
- Create: `PRIVACY.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `CHANGELOG.md`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: current CLI commands, WebUI behavior and setup script parameters.
- Produces: an install path a new Windows user can follow without private files.

- [ ] Add failing policy assertions for current feature names, public quick-start files, privacy disclosure and sensitive suffix ignores.
- [ ] Replace outdated batch language and absolute paths with the current product architecture and copy-pasteable fake/real flows.
- [ ] Document LLM data transfer, local API-key persistence, model import, trained-voice ownership and separate model licenses.
- [ ] Add governance documents and safe ignore patterns with explicit exceptions for examples.
- [ ] Run repository policy tests and Markdown link/path checks.

### Task 5: GitHub automation and templates

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Create: `.github/pull_request_template.md`
- Create: `.github/dependabot.yml`

**Interfaces:**
- Consumes: `uv.lock`, Python 3.11 and existing test markers.
- Produces: deterministic Windows CI that never needs models or repository secrets.

- [ ] Add failing policy assertions for workflow triggers, Windows runner, Python 3.11, frozen sync, lint, type, JavaScript, non-GPU tests and build.
- [ ] Add the least-privilege workflow with `permissions: contents: read` and concurrency cancellation.
- [ ] Add structured issue forms that ask for sanitized diagnostics and forbid private audio or keys.
- [ ] Validate all YAML with `yaml.safe_load` and run the policy tests.

### Task 6: Release verification

**Files:**
- Modify as required by failures discovered during verification.

**Interfaces:**
- Consumes: the complete tracked repository.
- Produces: clean verification evidence and a release-ready commit.

- [ ] Run `ruff check src tests`, `mypy src workers`, `node --check src/voice_pipeline/webui/app.js` and the full non-GPU pytest selection.
- [ ] Run `uv build --wheel` and inspect the wheel for WebUI plus license/notice files.
- [ ] Export only tracked files into a temporary directory, run `uv sync --frozen --extra dev`, start fake mode on a temporary port, verify `/api/v1/health`, then stop it.
- [ ] Scan current history for common secrets and list all historical blobs by size; fail if a secret candidate or blob over 10 MiB is present.
- [ ] Confirm `git status --short` is empty and commit the verified public-release preparation.
