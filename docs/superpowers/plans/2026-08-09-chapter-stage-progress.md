# Chapter Stage Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在章节历史上方增加由真实章节/分块状态派生的四阶段进度条。

**Architecture:** 将进度派生放进无 DOM 依赖的 ES module，WebUI 只负责渲染和维护 POST 返回前的临时 planning 状态。继续复用现有 run/progress/SSE 数据，不新增数据库字段或 API schema。

**Tech Stack:** Vanilla JavaScript ES modules、HTML/CSS、FastAPI 静态资源路由、pytest、Node.js。

## Global Constraints

- 阶段固定为 `文本规划`、`参考音频`、`GSV 合成`、`整篇拼接`。
- 总体百分比为四个阶段完成比例的算术平均值。
- 成功章节保持 100%，草稿编辑和局部重生成不使历史任务进度倒退。
- 不修改数据库、章节 API schema、GPU 队列或推理流程。
- 所有新行为先写测试并观察预期失败。

---

### Task 1: 纯进度派生模块

**Files:**
- Create: `src/voice_pipeline/webui/stage-progress.js`
- Modify: `src/voice_pipeline/api/workbench_routes.py`
- Create: `tests/unit/test_stage_progress_js.py`

**Interfaces:**
- Produces: `TASK_STAGE_DEFINITIONS` 和 `deriveChapterStageProgress(run, progress, creationState)`。
- Output: `{overallPercent, statusLabel, activeStage, stages}`；每个 stage 含 `key`, `label`, `ratio`, `detail`, `state`。

- [ ] **Step 1: Write failing pure-function tests**

用 Node 动态导入模块，覆盖无任务、planning、部分 reference/GSV、等待拼接、成功、失败六种输入；核心断言示例：

```python
assert result["overallPercent"] == 50
assert result["stages"][1]["detail"] == "1/2"
assert result["activeStage"] == "reference"
```

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_stage_progress_js.py
```

Expected: FAIL because `stage-progress.js` does not exist or is not served.

- [ ] **Step 3: Implement minimal derivation**

```js
export const TASK_STAGE_DEFINITIONS = [
  ["planning", "文本规划"],
  ["reference", "参考音频"],
  ["gsv", "GSV 合成"],
  ["compose", "整篇拼接"],
];

export function deriveChapterStageProgress(run, progress, creationState = null) {
  // Count active version ids, derive active job kind, and average four ratios.
}
```

Add `stage-progress.js` to `_WEBUI_FILES`.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 pytest command; expected PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/voice_pipeline/webui/stage-progress.js src/voice_pipeline/api/workbench_routes.py tests/unit/test_stage_progress_js.py
git commit -m "feat: derive chapter stage progress"
```

### Task 2: WebUI stage tracker

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: `deriveChapterStageProgress` and `TASK_STAGE_DEFINITIONS` from Task 1.
- Produces: `renderChapterProgress()` and DOM mount `#chapter-progress`.

- [ ] **Step 1: Write failing static contract assertions**

Assert the page exposes `id="chapter-progress"` before the chapter-history heading, the app imports the module, renders `role="progressbar"`, and styles complete/active/failed states.

- [ ] **Step 2: Verify RED**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/contract/test_workbench_api.py
```

Expected: FAIL because the progress mount and renderer are missing.

- [ ] **Step 3: Add markup and renderer**

Insert the mount before the history heading:

```html
<section id="chapter-progress" class="chapter-progress" aria-label="任务阶段进度"></section>
```

Import the pure module and render a heading, segmented rail, and stage list. Set `role="progressbar"`, `aria-valuemin="0"`, `aria-valuemax="100"`, and `aria-valuenow` whenever a run exists.

- [ ] **Step 4: Wire live updates**

Call `renderChapterProgress()` from `renderRunDetails()`. Set a local creation state before POST, clear it after a successful `selectRun`, and retain a failed planning state if POST rejects. Existing SSE/refresh already calls `renderRunDetails()`, so no second polling mechanism is added.

- [ ] **Step 5: Add responsive CSS**

Use four segmented rail cells and a two-column stage detail grid; active cells animate only when reduced motion is not requested. Completed is green, active blue, failed red, pending gray.

- [ ] **Step 6: Verify GREEN**

Run contract test, `node --check`, Ruff and mypy.

- [ ] **Step 7: Commit**

```powershell
git add src/voice_pipeline/webui/index.html src/voice_pipeline/webui/app.js src/voice_pipeline/webui/styles.css tests/contract/test_workbench_api.py
git commit -m "feat: show chapter stage progress in workbench"
```

### Task 3: Regression and local deployment

**Files:**
- No production files expected.

- [ ] **Step 1: Full non-GPU regression**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q -m "not gpu and not gpu_residency and not quality_model"
```

Expected: all tests pass.

- [ ] **Step 2: Build and install committed wheel**

Build a wheel under `runtime/build/<commit>/`, force-install it into `.venv-control`, gracefully stop the old service, then start `config/acceptance.gpu.local.yaml`.

- [ ] **Step 3: Browser verification**

Reload `http://127.0.0.1:8765/`; verify the stage tracker is above chapter history, has four labels, exposes accessible progress attributes, and does not overflow the sidebar.

- [ ] **Step 4: Final health verification**

Confirm `/api/v1/health` is `ready`, storage migration remains `0003_chapter_history_soft_delete`, dispatcher is `running`, and Git is clean.
