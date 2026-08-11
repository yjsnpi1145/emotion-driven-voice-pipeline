# ASR Text Scoring Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted WebUI switch that disables only ASR text-similarity rejection for future reference jobs.

**Architecture:** Wrap the existing quality analyzer in a runtime gate that post-processes reports and owns one atomic local settings file. Expose the gate through a small REST settings contract and bind it to an accessible header toggle; the synthesis pipeline and version store continue consuming `QualityReport` without a parallel path.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, asyncio, vanilla JavaScript, pytest, Ruff, Mypy.

## Global Constraints

- ASR-off must never bypass WAV duration, speech duration, speech ratio, or VAD failures.
- Default behavior remains ASR text scoring enabled.
- Existing enabled-mode policy fingerprints remain unchanged.
- Runtime settings contain no user audio, transcript, API key, or local model path.
- The setting applies server-side and survives browser refresh and service restart.

---

### Task 1: Runtime quality gate

**Files:**
- Create: `src/voice_pipeline/modules/quality/runtime.py`
- Modify: `src/voice_pipeline/models/runtime_settings.py`
- Modify: `src/voice_pipeline/modules/quality/ports.py`
- Test: `tests/unit/test_runtime_quality_settings.py`

**Interfaces:**
- Produces: `QualityScoringSettingsUpdate`, `QualityScoringSettingsView`.
- Produces: `RuntimeQualityGate.start()`, `view()`, `update()`, `analyze_reference()`, `accepts_saved_report()`.

- [ ] Write failing tests for default enabled behavior, text-failure bypass, VAD preservation, atomic persistence, and accepted report fingerprints.
- [ ] Run `\.venv\Scripts\python.exe -m pytest tests/unit/test_runtime_quality_settings.py -q` and confirm missing imports fail.
- [ ] Implement the smallest wrapper, disabled fingerprint derivation, report transformation, and atomic JSON persistence.
- [ ] Re-run the unit tests and refactor only while green.

### Task 2: Pipeline compatibility and cache isolation

**Files:**
- Modify: `src/voice_pipeline/core/pipeline.py`
- Test: `tests/unit/test_pipeline.py`

**Interfaces:**
- Consumes: `RuntimeQualityGate.accepts_saved_report(report)`.
- Guarantees: enabled/disabled cache keys do not collide; reports generated under either toggle remain usable as immutable references.

- [ ] Add failing tests for disabled text mismatch success, disabled VAD failure, old strict report reuse while disabled, and disabled report reuse after re-enable.
- [ ] Run the focused test file and confirm current policy fingerprint validation fails the compatibility cases.
- [ ] Update cache publication to use the report’s actual fingerprint and saved-report validation to call the gate capability when present.
- [ ] Re-run the focused tests.

### Task 3: Application lifecycle, REST settings, and health

**Files:**
- Modify: `src/voice_pipeline/api/app.py`
- Modify: `src/voice_pipeline/api/product_routes.py`
- Modify: `src/voice_pipeline/api/routes.py`
- Test: `tests/contract/test_product_settings_api.py`
- Test: `tests/integration_cpu/test_app_storage_lifecycle.py`

**Interfaces:**
- Produces: `GET/PUT /api/v1/settings/quality`.
- Produces: `health.quality.asr_text_scoring_enabled`.

- [ ] Add failing API tests for default, update, unknown-field rejection, and restart persistence.
- [ ] Add a failing health assertion.
- [ ] Wrap the selected fake/real analyzer during lifespan startup and register the new endpoints.
- [ ] Re-run contract and lifecycle tests.

### Task 4: Header toggle

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `src/voice_pipeline/webui/app.js`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: `#asr-scoring-toggle` beside `#shutdown-services`.
- Consumes: `/api/v1/settings/quality`.

- [ ] Add failing static contract assertions for markup, request paths, initialization, status copy, and CSS states.
- [ ] Run the workbench contract test and confirm it fails.
- [ ] Implement load/save/rollback behavior, accessibility labels, responsive dark-console styling, and bump static asset versions.
- [ ] Run the contract test and `node --check src/voice_pipeline/webui/app.js`.

### Task 5: Verification, live acceptance, and release

**Files:**
- Modify only if verification exposes a feature defect.

- [ ] Run focused tests, Ruff, Mypy, and the full non-GPU/non-quality-model suite.
- [ ] Push the feature branch, create a PR, wait for Windows CI, and merge.
- [ ] Build/install merged `main`, restart all local services, and confirm health/UI settings agree.
- [ ] Disable ASR scoring through the public API, submit a real reference whose expected text deliberately differs while duration/VAD remain valid, and prove the job succeeds with `text_skipped`.
- [ ] Restore the final switch state selected for normal user operation and leave the WebUI ready.
