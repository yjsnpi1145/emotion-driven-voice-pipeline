# Chapter GSV Archive Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a WebUI action that downloads every segment's current GSV WAV in one verified ZIP archive.

**Architecture:** A focused `ChapterGsvArchiveBuilder` freezes the ordered current GSV versions, validates their content-addressed blobs, and writes a temporary ZIP plus a path-free manifest. The chapter API serves the archive with post-response cleanup; the WebUI exposes a button only when every segment has a current GSV pointer.

**Tech Stack:** Python 3.11, FastAPI/Starlette, SQLAlchemy-backed stores, stdlib `zipfile`, vanilla JavaScript, pytest/httpx.

## Global Constraints

- Export current active GSV versions only; never silently omit a segment.
- Do not expose absolute local paths in API payloads, response headers, or `manifest.json`.
- Verify version ownership, artifact type/state, canonical location, regular-file status, and SHA-256 before archiving.
- Use stdlib ZIP support; add no runtime dependency.
- Build the ZIP off the asyncio event loop and remove temporary archives after response completion or failure.
- Keep existing chapter audio, timeline, compose, regeneration, and history contracts unchanged.

---

### Task 1: Lock the archive API contract with failing integration tests

**Files:**
- Modify: `tests/contract/test_chapter_api.py`

**Interfaces:**
- Consumes: existing chapter submission, progress, and version APIs.
- Produces: expectations for `GET /api/v1/chapters/{run_id}/export/gsv`.

- [ ] **Step 1: Write a failing successful-export test**

Extend the completed two-segment chapter test to request the new endpoint, open `response.content` with `zipfile.ZipFile(io.BytesIO(...))`, and assert exact members `001.wav`, `002.wav`, `manifest.json`. Assert each WAV starts with `RIFF`; manifest entries match progress active version IDs in ordinal order and contain no private base voice or artifact root.

- [ ] **Step 2: Write failing incomplete/corrupt tests**

While a gated chapter has no GSV pointers, assert HTTP 409, error code `CHAPTER_STATE_CONFLICT`, and exact `missing_ordinals`. In a completed chapter, mutate an active GSV canonical blob and assert HTTP 409 with `ARTIFACT_CORRUPT` and no ZIP response.

- [ ] **Step 3: Run RED tests**

Run:

```powershell
.venv-control\Scripts\python.exe -m pytest tests/contract/test_chapter_api.py -q
```

Expected: new assertions fail with HTTP 404 because the export route does not exist.

### Task 2: Implement the secure archive builder and route

**Files:**
- Create: `src/voice_pipeline/core/chapter_export.py`
- Modify: `src/voice_pipeline/api/chapter_routes.py`
- Test: `tests/contract/test_chapter_api.py`

**Interfaces:**
- Produces: `ChapterGsvArchiveBuilder.build(run_id: UUID) -> ChapterGsvArchive`.
- Produces: `ChapterGsvArchive(path: Path, download_name: str)` and idempotent `cleanup()`.
- Exposes: `GET /api/v1/chapters/{run_id}/export/gsv` returning `application/zip`.

- [ ] **Step 1: Freeze and validate entries**

Load the run and its ordered segments. Reject all missing active GSV pointers in one `PipelineError` with `details={"missing_ordinals": [...]}`. For each pointer, require matching segment ownership, `artifact_type == "gsv"`, `state == "ready"`, canonical content-addressed relative path, non-symlink regular WAV, and matching `sha256_file`.

- [ ] **Step 2: Write the temporary ZIP off-loop**

Use `asyncio.to_thread` and stdlib `zipfile.ZipFile(..., ZIP_DEFLATED)`. Name files from one-based ordered positions with at least three digits. Serialize a UTF-8 `manifest.json` with schema version, run/title/time, ordered IDs, hashes, actual version synthesis text, language, and reference version ID. Delete a partially written archive on every exception.

- [ ] **Step 3: Serve and clean up**

Map unknown chapters to 404 and archive state/integrity errors to structured 409 responses. Return `FileResponse` with attachment filename and a Starlette `BackgroundTask` that unlinks the archive.

- [ ] **Step 4: Run GREEN tests**

Run the Task 1 command. Expected: all chapter API tests pass and the temporary export directory contains no completed response ZIP.

### Task 3: Add the WebUI export action test-first

**Files:**
- Modify: `tests/contract/test_workbench_api.py`
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css` only if layout needs a focused action-row rule.

**Interfaces:**
- Produces DOM element `#export-gsv-archive`.
- Consumes public progress field `active_gsv_version_id`.
- Navigates to `/api/v1/chapters/${state.run.run_id}/export/gsv` on an enabled click.

- [ ] **Step 1: Write and run the failing contract test**

Assert the page contains `id="export-gsv-archive"` and “导出全部分块 GSV”; assert the script contains the version-completeness check and export URL. Run the single workbench contract test and confirm it fails because the button is absent.

- [ ] **Step 2: Implement button state and download**

Place the secondary button beside compose. In `renderRunDetails`, disable it when no run, no segments, or any segment lacks `active_gsv_version_id`; otherwise set the title to explain current-version export. On click, initiate a same-origin browser download without reading ZIP bytes into JavaScript memory.

- [ ] **Step 3: Run the WebUI contract and Node syntax checks**

```powershell
.venv-control\Scripts\python.exe -m pytest tests/contract/test_workbench_api.py -q
node --check src/voice_pipeline/webui/app.js
```

Expected: both exit zero.

### Task 4: Full verification, real download, and publication

**Files:**
- Verify all modified files.

**Interfaces:**
- Consumes the real loopback service and an existing chapter with complete current GSV versions.
- Produces a merged GitHub update protected by `windows-python311`.

- [ ] **Step 1: Run static and complete non-GPU verification**

```powershell
.venv-control\Scripts\python.exe -m ruff check src tests
.venv-control\Scripts\python.exe -m mypy src workers
node --check src/voice_pipeline/webui/app.js
.venv-control\Scripts\python.exe -m pytest -q -m "not gpu and not gpu_residency and not quality_model"
git diff --check
```

Expected: zero failures and zero warnings that indicate broken behavior.

- [ ] **Step 2: Install and validate the real service**

Run `scripts/setup-control.ps1`, restart via the repository launcher, request an existing complete chapter's export endpoint, unzip it, verify member count/RIFF headers/manifest, and confirm `/api/v1/health` remains `ready`.

- [ ] **Step 3: Commit, push, PR, CI, merge**

Commit the implementation intentionally, push `codex/export-chapter-gsv-archive`, open a PR to `main`, wait for `windows-python311`, squash merge, and verify local `main` is clean and matches `origin/main`.
