# Director LLM Activity Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a scrollable, director-only LLM activity console to the persistent Director Mode WebUI.

**Architecture:** Reuse the existing `/api/v1/llm/activity` endpoint and existing console CSS. Add a small pure JavaScript module that filters director operations and derives status from operation lifecycles; keep network requests and safe DOM rendering in `director.js`.

**Tech Stack:** Native ES modules, HTML/CSS, FastAPI packaged static assets, pytest, Node.js syntax/evaluation tests.

## Global Constraints

- The UI remains loopback-only and must not add a Node build chain or CDN dependency.
- Only `script_analysis`, `cast_reconciliation`, and `script_translation` events appear in the director console.
- Event content is rendered with `textContent`; never use `innerHTML` for LLM output.
- No API Key, Authorization header, full prompt, or local secret is added to the activity response.
- The existing `/api/v1/llm/activity` response contract remains unchanged.
- Activity persistence remains in-memory and survives page refresh but not service restart.

---

### Task 1: Director Activity View Model

**Files:**
- Create: `src/voice_pipeline/webui/director-llm-activity.js`
- Create: `tests/unit/test_director_llm_activity_js.py`

**Interfaces:**
- Consumes: the existing activity snapshot `{active, active_operation, active_since_utc, events}`.
- Produces: `directorActivityView(snapshot, unavailable, nowMs)` returning `{events, active, activeSinceUtc, statusState, statusText}`.

- [ ] **Step 1: Write the failing pure JavaScript test**

Create a Node-driven pytest that imports the new module, supplies interleaved `chapter_plan`, `script_analysis`, and `script_translation` lifecycle events, and asserts that only director events remain, one unfinished operation keeps the status active, and terminal failure maps to `degraded`/`失败`.

- [ ] **Step 2: Run the test and verify RED**

Run: `uv run pytest tests/unit/test_director_llm_activity_js.py -q`

Expected: FAIL because `director-llm-activity.js` does not exist.

- [ ] **Step 3: Implement the minimal pure view model**

Implement an immutable allowed-operation set, filter events without mutating the snapshot, group the latest event by `operation_id`, treat operations whose latest kind is not `completed` or `failed` as active, and calculate elapsed seconds from the earliest active event. Return `连接异常`, `正在工作 · Ns`, `失败`, `已完成`, or `空闲` using the same state names as the existing console.

- [ ] **Step 4: Run the test and JavaScript syntax check**

Run:

```powershell
uv run pytest tests/unit/test_director_llm_activity_js.py -q
node --check src/voice_pipeline/webui/director-llm-activity.js
```

Expected: all checks pass.

### Task 2: Director Console Shell and Rendering

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `src/voice_pipeline/api/workbench_routes.py`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: `directorActivityView` from Task 1 and `GET /api/v1/llm/activity`.
- Produces: DOM nodes `#director-llm-activity-console`, `#director-llm-status`, and `#director-llm-log`.

- [ ] **Step 1: Write the failing static contract assertions**

Extend the workbench contract to request `/ui/director-llm-activity.js` and assert HTTP 200, the three director console IDs, the new module import, use of `textContent`, the existing activity endpoint, and `overflow-y: auto` through the reused `.llm-activity-log` rule.

- [ ] **Step 2: Run the contract test and verify RED**

Run: `uv run pytest tests/contract/test_workbench_api.py::test_workbench_serves_local_static_shell_and_public_chapter_listing -q`

Expected: FAIL because the new asset and director log node are absent.

- [ ] **Step 3: Implement the console shell and renderer**

Replace the compact director status strip with the shared console markup. Allow-list the new ES module. In `director.js`, preserve the existing events on transient fetch failure, call `directorActivityView`, render time/operation/message/content using created elements and `textContent`, and only scroll to the bottom when the user was already within 32 pixels of it.

- [ ] **Step 4: Run focused verification**

Run:

```powershell
uv run pytest tests/unit/test_director_llm_activity_js.py tests/contract/test_workbench_api.py -q
node --check src/voice_pipeline/webui/director.js
node --check src/voice_pipeline/webui/director-llm-activity.js
```

Expected: all checks pass.

- [ ] **Step 5: Run full static and CPU verification**

Run:

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src workers
uv run pytest -m "not gpu and not gpu_residency and not quality_model" -q
```

Expected: all checks pass with no new skips.

- [ ] **Step 6: Perform live browser acceptance**

Start the local service, open Director Mode, verify the director-only scroll console at desktop and 390-pixel responsive widths, confirm there is no horizontal overflow, and confirm browser console logs contain no errors.

- [ ] **Step 7: Commit and push**

Stage only the design, plan, new module, tests, and four modified product files. Commit with `feat: show LLM output in director mode`, push `codex/director-llm-console`, open a PR against `main`, and wait for CI.
