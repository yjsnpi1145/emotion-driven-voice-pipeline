# Chapter History Delete and Audio Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe chapter-history deletion and make every native audio control display duration before playback.

**Architecture:** Add one nullable soft-delete timestamp through Alembic and filter it in `ChapterStore`; expose a terminal-state-only DELETE endpoint. Render history rows as select-plus-delete controls, and replace every audio `preload=none` setting with metadata preload.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy/Alembic/SQLite, vanilla JavaScript/CSS, pytest.

## Global Constraints

- Running or queued chapter runs cannot be deleted.
- Deletion preserves tasks, segments, jobs, versions and WAV files.
- Deleted runs are excluded from all chapter lookup paths.
- Audio endpoints and Range behavior remain unchanged; only browser preload policy changes.

---

### Task 1: Add durable chapter-history soft deletion

**Files:**
- Create: `src/voice_pipeline/storage/migrations/versions/0003_chapter_history_soft_delete.py`
- Modify: `src/voice_pipeline/storage/orm.py`
- Modify: `src/voice_pipeline/storage/database.py`
- Modify: `src/voice_pipeline/storage/chapter_store.py`
- Test: `tests/integration_cpu/test_chapter_store.py`

**Interfaces:**
- Produces: `ChapterStore.delete_history_entry(run_id: UUID) -> None`.

- [ ] Write failing tests proving terminal deletion hides get/list while preserving segments, and queued deletion raises a state conflict.
- [ ] Run `pytest -q tests/integration_cpu/test_chapter_store.py` and observe missing method/schema failures.
- [ ] Add `deleted_at_utc`, migration `0003_chapter_history_soft_delete`, filtering and terminal-state update.
- [ ] Re-run the focused tests and confirm pass.

### Task 2: Expose DELETE and add the history-row control

**Files:**
- Modify: `src/voice_pipeline/api/chapter_routes.py`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Test: `tests/contract/test_chapter_api.py`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: `DELETE /api/v1/chapters/{run_id}` returning status JSON.

- [ ] Write failing API tests for successful deletion, post-delete 404 and active-run 409.
- [ ] Add static shell assertions for DELETE usage, confirmation copy and delete-button class.
- [ ] Implement endpoint and selected-run-safe UI state reset.
- [ ] Run both contract files and confirm pass.

### Task 3: Load audio metadata

**Files:**
- Modify: `src/voice_pipeline/webui/app.js`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: all native players using `preload="metadata"` or `player.preload = "metadata"`.

- [ ] Add failing assertions that metadata preload exists and none preload does not.
- [ ] Replace all three player construction paths: final audio, current/version string template and history player.
- [ ] Run the static shell contract and confirm pass.

### Task 4: Verify and deploy

**Files:** all modified files above.

- [ ] Run Ruff, mypy, Node syntax and `git diff --check`.
- [ ] Run `pytest -q -m "not gpu and not gpu_residency and not quality_model"` with zero failures.
- [ ] Commit with `feat: add chapter history deletion and audio metadata preload`.
- [ ] Build a commit-specific wheel, install into `.venv-control`, restart with `config/acceptance.gpu.local.yaml`, and wait for ready health.
- [ ] Verify the served page includes delete controls, installed database head is `0003_chapter_history_soft_delete`, and version Range GET still returns 206.
