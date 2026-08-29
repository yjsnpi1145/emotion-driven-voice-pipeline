# Director Performance Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist project-wide performance guidance, apply it only to contextual performance
parameters, and provide a compact per-utterance adjustment dialog with safe local regeneration.

**Architecture:** Add one nullable project column and carry it as a top-level field into the
contextual emotion request. Add a Director adjustment command that synchronizes the reviewed
utterance and its materialized segment, computes dependency invalidation, and schedules a focused
reference/GSV/recompose runner. Replace the inline editor with one reusable native dialog driven by
the existing Director polling state.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, SQLAlchemy/Alembic, vanilla ES modules and CSS,
pytest, Node-based frontend unit tests.

## Global Constraints

- `performance_direction` is optional, persisted, whitespace-normalized, and at most 2,000 Unicode
  characters.
- Global direction may change only emotion vector, speed factor, and pause after milliseconds.
- Emotion order and total `<= 0.8` remain unchanged.
- Existing projects migrate with a null direction and no artifact loss.
- Generated versions are immutable; active bindings may change only after successful jobs.
- Existing whole-project generation, resume, recompose, and archive contracts remain compatible.
- All production behavior is implemented test-first.

---

### Task 1: Persist and edit project performance direction

**Files:**
- Modify: `src/voice_pipeline/models/director.py`
- Modify: `src/voice_pipeline/storage/orm.py`
- Create: `src/voice_pipeline/storage/migrations/versions/0009_director_performance_controls.py`
- Modify: `src/voice_pipeline/storage/database.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Modify: `src/voice_pipeline/api/director_routes.py`
- Modify: `tests/integration_cpu/test_director_migration.py`
- Modify: `tests/contract/test_director_api.py`

**Interfaces:**
- `CreateDirectorProjectRequest.performance_direction: str | None`
- `DirectorProjectRecord.performance_direction: str | None`
- `UpdateDirectorPerformanceDirection(expected_revision, performance_direction, reapply=False)`
- `DirectorStore.update_performance_direction(...) -> DirectorProjectRecord`
- `PATCH /api/v1/director-projects/{project_id}/performance-direction`

- [ ] Write a migration test that upgrades an existing 0008 database, asserts the new nullable
  column, preserves an existing project, and reports packaged head
  `0009_director_performance_controls`.
- [ ] Run `pytest tests/integration_cpu/test_director_migration.py -q` and observe the packaged-head
  and missing-column failures.
- [ ] Add the ORM column and idempotent Alembic migration, update `PACKAGED_HEAD`, and map the column
  in project create/read code.
- [ ] Write contract tests proving create/read persistence, whitespace-to-null normalization,
  2,001-character rejection, optimistic conflict, and an audit event on update.
- [ ] Run the new contract tests and observe missing request/route failures.
- [ ] Implement the strict request model and revision-guarded store/API update. Permit direct edits
  through `role_review`; require `reapply=true` after translation and return the affected spoken
  count in the response.
- [ ] Run migration and Director contract tests, Ruff the touched files, and commit
  `feat: persist Director performance guidance`.

### Task 2: Direct vector, speed, and pause without changing text

**Files:**
- Modify: `src/voice_pipeline/models/director_llm.py`
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/modules/llm/fake.py`
- Modify: `src/voice_pipeline/modules/llm/runtime.py`
- Modify: `src/voice_pipeline/core/director_analysis.py`
- Modify: `tests/unit/test_llm_client.py`
- Modify: `tests/integration_cpu/test_director_analysis.py`

**Interfaces:**
- `EmotionDirectionResultItem` adds bounded `speed_factor` and `pause_after_ms`.
- `direct_emotions(*, performance_direction: str | None, utterances: tuple[...])`.
- `_apply_directed_performance(...)` copies only vector, speed, and pause.

- [ ] Extend the LLM request-contract test to expect top-level `performance_direction`, explicit
  soft-bias instructions, and result speed/pause fields; run it and observe signature/schema
  failures.
- [ ] Extend the integration Director double so translation returns sentinel text/reference while
  emotion direction returns a different vector, speed, and pause; assert only the three performance
  fields change and text remains byte-for-byte equal.
- [ ] Run the integration test and observe missing replacement failures.
- [ ] Implement model/client/runtime/fake signatures and prompt boundaries. Pass the project field
  once per batch and preserve empty-direction behavior.
- [ ] Rename/extend the merge helper to validate one-to-one identities and copy only the three
  permitted fields.
- [ ] Add a test for post-translation reapply that reruns direction without calling translation,
  preserves text, increments revisions, and records invalidation requirements for spoken rows.
- [ ] Run focused LLM/Director tests and commit
  `feat: apply global Director performance guidance`.

### Task 3: Atomic utterance adjustment and focused regeneration

**Files:**
- Modify: `src/voice_pipeline/models/director.py`
- Create: `src/voice_pipeline/core/director_adjustment.py`
- Modify: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Modify: `src/voice_pipeline/api/director_routes.py`
- Modify: `src/voice_pipeline/api/app.py`
- Create: `tests/unit/test_director_adjustment.py`
- Modify: `tests/integration_cpu/test_director_end_to_end.py`
- Modify: `tests/contract/test_director_api.py`

**Interfaces:**
- `DirectorAdjustmentAction = Literal["save", "reference", "gsv", "both", "recompose"]`
- `AdjustDirectorUtteranceRequest` contains expected project/utterance revisions and all five
  editable performance fields.
- `resolve_adjustment(changed_fields, requested_action, reference_valid) -> effective_action`.
- `DirectorGenerationService.adjust_utterance(...) -> DirectorGenerationRecord | None`.
- `POST /api/v1/director-projects/{project_id}/utterances/{utterance_id}/adjust`.

- [ ] Write pure failing tests for invalidation: reference/emotion => both stale, synthesis/speed =>
  GSV stale, pause => compose stale, and requested GSV => effective both when reference is stale.
- [ ] Implement a small immutable `AdjustmentDecision` and pure `resolve_adjustment` function; run
  unit tests to green.
- [ ] Write contract tests for validation, version conflicts, legal states, duplicate active
  submission, and the accepted payload containing requested/effective action.
- [ ] Write integration tests using fake Index/GSV engines for save-only, reference-only, GSV-only,
  both, and pause-only recomposition. Assert old artifact versions remain readable and only active
  bindings change.
- [ ] Run those tests and observe missing route/service failures.
- [ ] Implement adjustment orchestration: patch the utterance and materialized segment using their
  current draft revisions, set the generation item to `queued`/`reference_ready` as dictated by the
  decision, and mark the generation incomplete on save-only.
- [ ] Extract focused reference, GSV, and compose helpers from the existing full runner without
  changing full-run behavior. Track one task per `(generation_id, utterance_id)` and return the
  existing task result for duplicate submissions.
- [ ] Preserve pooled-reference preparation by calling the existing `_prepare_reference` path.
  Auto-compose after a ready GSV and persist failures on the item for retry.
- [ ] Run all Director generation, pool, API, and end-to-end tests; Ruff and mypy the touched source;
  commit `feat: regenerate individual Director utterances`.

### Task 4: Compact project guidance and per-utterance dialog

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Create: `src/voice_pipeline/webui/director-adjustment.js`
- Create: `tests/unit/test_director_adjustment_js.py`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- `buildAdjustmentPayload(utterance, draft, action, projectRevision)` returns the API body.
- `deriveAdjustmentAvailability(project, progressItem, dirtyFields)` returns enabled actions and
  escalation copy.
- One `#director-adjustment-dialog` native dialog is reused for all utterances.

- [ ] Write Node tests for dirty-field detection, slider/number normalization, total-vector
  validation, action availability, GSV escalation copy, and passive-refresh draft preservation.
- [ ] Run `pytest tests/unit/test_director_adjustment_js.py -q` and observe missing-module failures.
- [ ] Implement the pure ES module helpers and rerun to green.
- [ ] Add contract assertions for the optional 2,000-character project textarea, shared dialog,
  audio elements, five action buttons, and static asset delivery.
- [ ] Add the creation payload and revision-guarded project guidance save/reapply controls. Require
  confirmation before a post-translation reapply.
- [ ] Remove the inline expanding editor. Add one compact `调整配音` button to spoken cards from
  translation review onward and populate the shared dialog from current utterance/progress data.
- [ ] Implement synchronized range/number controls, dirty-close confirmation, retained drafts,
  structured error display, current reference/GSV URLs, and background polling refresh.
- [ ] Wire all five actions to the adjustment endpoint; keep the modal open on errors and show the
  effective-action escalation returned by the server.
- [ ] Run frontend unit/contract tests and a browser smoke at desktop and narrow widths; commit
  `feat: add Director utterance adjustment dialog`.

### Task 5: Full verification, merge, and deployment

**Files:**
- Modify only regression files for defects first reproduced by a failing test.

- [ ] Run `ruff check .` and focused mypy on every changed source file.
- [ ] Stop the supported local service and run
  `pytest -m "not gpu and not gpu_residency and not quality_model" -q`.
- [ ] Merge the feature branch into `main`, rerun the complete non-GPU suite on the merged commit,
  build the wheel, force-install it into `.venv-control`, and start via `scripts/start.ps1`.
- [ ] Confirm health reports `ready` and Alembic revision `0009_director_performance_controls`.
- [ ] Create a real Director project with a calm global direction, verify LLM activity and bounded
  vector/speed/pause outputs, complete one generated sentence, open the adjustment dialog, perform
  one GSV-only local regeneration, verify a new active version and successful recomposition, then
  delete the smoke project.

## Self-review

The plan covers every persisted field, schema boundary, state transition, dependency escalation,
pooled short-reference case, accessible UI control, audit requirement, test layer, deployment step,
and real-service acceptance criterion from the approved design. Interface names and action literals
are consistent across tasks and no implementation placeholders remain.
