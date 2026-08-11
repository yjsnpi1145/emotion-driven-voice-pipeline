# Resume Failed Chapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable “repair then resume” chapter workflow that reuses valid segment audio and continues from the first unresolved segment.

**Architecture:** Extend `ChapterStore` with one atomic resume transition, then teach `ChapterService` to reconstruct immutable inputs from the saved chapter snapshot and branch per segment from durable progress state. Expose one HTTP endpoint and one WebUI action; all generated jobs continue through the existing segment job/version pipeline.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, async SQLAlchemy/SQLite, vanilla JavaScript, pytest, Ruff, Mypy.

## Global Constraints

- Only `failed` and `interrupted` chapter runs may resume.
- A valid current GSV version is never regenerated.
- A known failed or stale reference must be repaired explicitly before resume.
- Never expose local paths in public HTTP responses.
- Do not add a second job system or a WebUI-only state store.

---

### Task 1: Atomic chapter resume state transition

**Files:**
- Modify: `src/voice_pipeline/storage/chapter_store.py`
- Test: `tests/integration_cpu/test_chapter_store.py`

**Interfaces:**
- Produces: `ChapterStore.mark_resuming(run_id: UUID) -> ChapterRunRecord`
- Guarantees: one `failed|interrupted -> running` compare-and-set; clears `error_json` and `finished_at_utc`.

- [ ] Add failing tests for failed and interrupted transitions and rejection of an already running run.
- [ ] Run `\.venv\Scripts\python.exe -m pytest tests/integration_cpu/test_chapter_store.py -q` and confirm the new tests fail because `mark_resuming` is absent.
- [ ] Implement the minimal conditional update and public `CHAPTER_STATE_CONFLICT` error.
- [ ] Re-run the focused test file and confirm it passes.

### Task 2: Snapshot reconstruction and resumable orchestration

**Files:**
- Modify: `src/voice_pipeline/core/chapter_service.py`
- Test: `tests/integration_cpu/test_chapter_pipeline.py`
- Test: `tests/unit/test_chapter_service_validation.py`

**Interfaces:**
- Produces: `ChapterService.resume(run_id: UUID) -> ChapterRunRecord`
- Produces: one background continuation that skips `gsv_state=ready`, reuses `reference_state=ready`, and generates untouched later segments.
- Raises: `PipelineError(CHAPTER_STATE_CONFLICT)` with `ordinal`, `segment_id`, and `action_required` when a failed/stale reference still blocks continuation.

- [ ] Add a failing integration test that creates a failed run with a repaired reference and asserts the continuation does not invoke IndexTTS for completed/repaired segments.
- [ ] Add failing validation tests for missing/changed base voice, failed reference precondition, active regeneration, and duplicate resume.
- [ ] Run the focused tests and confirm failures are caused by the missing service behavior.
- [ ] Implement snapshot parsing, digest validation, preflight, background task registration, and resumable per-segment branching.
- [ ] Re-run focused tests and refactor only while green.

### Task 3: HTTP resume contract

**Files:**
- Modify: `src/voice_pipeline/api/chapter_routes.py`
- Test: `tests/contract/test_chapter_api.py`

**Interfaces:**
- Produces: `POST /api/v1/chapters/{run_id}/resume` with 202 envelope.
- Maps: unknown run to 404, conflicts to 409, non-accepting plane to 503.

- [ ] Add failing contract tests for accepted resume and unrepaired reference conflict.
- [ ] Run `\.venv\Scripts\python.exe -m pytest tests/contract/test_chapter_api.py -q` and confirm 404/missing route failures.
- [ ] Add the route using existing public error and envelope shapes.
- [ ] Re-run the contract tests and confirm they pass.

### Task 4: WebUI continuation action

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: button `#resume-chapter` and `resumeChapter()`.
- Consumes: `state.run.status` and `POST /api/v1/chapters/{run_id}/resume`.

- [ ] Add failing static shell assertions for the button, endpoint string, and failed/interrupted enablement.
- [ ] Run `\.venv\Scripts\python.exe -m pytest tests/contract/test_workbench_api.py -q` and confirm the assertions fail.
- [ ] Add button rendering, enable/title rules, busy handling, request, refresh, status message, and static asset version bump.
- [ ] Run the contract test and `node --check src/voice_pipeline/webui/app.js`.

### Task 5: Full verification and live acceptance

**Files:**
- Modify only if verification exposes a feature defect.

- [ ] Run Ruff over `src`, `workers`, and `tests`.
- [ ] Run Mypy over `src` and `workers`.
- [ ] Run all non-GPU/non-quality-model tests.
- [ ] Build and install the current commit into `.venv-control`, restart the real local stack, and wait for `/api/v1/health` to report ready.
- [ ] Call resume on the known failed chapter before repair and verify no duplicate generation is launched.
- [ ] Repair that segment's reference, call resume again, and verify completed prefix segments retain their version IDs while the chapter proceeds.
- [ ] Commit, push the `codex/resume-failed-chapters` branch, merge the pull request after CI, reinstall merged `main`, and restart all services.
