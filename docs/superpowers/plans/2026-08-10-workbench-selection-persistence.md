# Workbench Selection Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement each task test-first and verify RED before production edits.

**Goal:** 刷新工作台后恢复最后选择的章节与分块，并重新加载服务端真实进度和 SSE。

**Architecture:** 新建无 DOM 依赖的 `selection-state.js`，负责版本化 localStorage payload、校验和章节回退选择；`app.js` 只在成功加载/选择后写入 ID 指针。SQLite/API 继续是任务状态的唯一事实来源。

**Tech Stack:** Vanilla JavaScript ES modules、FastAPI 静态资源、Node.js、pytest。

## Global Constraints

- localStorage 只保存 `schema_version`、`run_id`、`segment_id`。
- 不保存文案、路径、音频、状态、模型参数或密钥。
- 存储异常必须静默降级，不能阻断页面初始化。
- 不修改数据库和 REST API。
- 所有新行为先写测试并观察预期失败。

---

### Task 1: 版本化选择状态模块

**Files:**
- Create: `src/voice_pipeline/webui/selection-state.js`
- Create: `tests/unit/test_selection_state_js.py`
- Modify: `src/voice_pipeline/api/workbench_routes.py`

**Interfaces:**
- `readWorkbenchSelection(storage)` -> `{runId, segmentId}`
- `writeWorkbenchSelection(storage, {runId, segmentId})` -> `boolean`
- `clearWorkbenchSelection(storage)` -> `boolean`
- `chooseInitialRunId(chapters, savedRunId)` -> `string | null`

- [ ] 写 Node 驱动的失败测试，覆盖 round-trip、损坏 JSON、storage 抛错、保存任务命中、运行任务回退、空列表。
- [ ] 运行测试并确认因模块不存在而失败。
- [ ] 实现 schema 1 payload、UUID 校验和异常隔离。
- [ ] 将新模块加入 `_WEBUI_FILES` 白名单并运行测试至通过。

### Task 2: 章节与分块恢复

**Files:**
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- `selectRun(runId, {preferredSegmentId = null} = {})`
- `persistCurrentSelection()`

- [ ] 先增加静态契约断言：导入选择模块、初始化读取指针、`selectRun` 接受 preferred segment、成功后写入、删除时清除。
- [ ] 运行契约测试并确认缺少上述集成而失败。
- [ ] 初始化时用 `chooseInitialRunId()` 选择恢复任务；仅当保存 run 匹配时传入保存 segment。
- [ ] `selectRun()` 从服务器返回的 segments 中恢复分块，加载历史后写入选择。
- [ ] `selectSegment()` 与 `refreshRun()` 成功后更新选择。
- [ ] 删除当前章节及空列表时清除失效选择。
- [ ] 更新静态资源版本并运行定向测试至通过。

### Task 3: 回归和本地部署

**Files:**
- No production files expected.

- [ ] 运行 Ruff、mypy、Node syntax check 和全部非 GPU 测试。
- [ ] 提交代码、构建当前提交 wheel、安装到 `.venv-control`。
- [ ] 重启真实本地服务并确认 health 为 `ready`。
- [ ] 在 WebUI 选择章节/分块、刷新页面，验证选择和进度恢复，前端控制台无错误。
