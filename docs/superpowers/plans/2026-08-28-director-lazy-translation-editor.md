# 导演模式译文编辑器按需展开 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让勾选“配音”后的语句卡片保持紧凑，仅在用户主动展开时创建译文与情绪编辑表单。

**Architecture:** 新增无 DOM 依赖的懒挂载状态辅助模块，并由 `director.js` 使用 `<details>` 容器按需挂载现有 `translatedEditor()`。关闭面板时卸载表单，现有 `dirtyTranslations` 继续保存未提交输入。

**Tech Stack:** 原生 ES modules、原生 DOM、Node.js 单元测试、FastAPI/httpx 合同测试、CSS。

## Global Constraints

- 配音复选框只改变 `speak_enabled`，不得直接创建完整译文表单。
- 翻译表单默认不挂载；展开一项时只挂载该项，收起时卸载。
- 未保存输入必须继续通过 `directorState.dirtyTranslations` 恢复。
- 新 ES module 必须加入 `_WEBUI_FILES` 并有 HTTP 200 合同测试。
- 不引入新前端依赖，不改变后端数据模型。

---

### Task 1: 可测试的懒挂载状态模块

**Files:**
- Create: `src/voice_pipeline/webui/director-lazy-editor.js`
- Create: `tests/unit/test_director_lazy_editor_js.py`
- Modify: `src/voice_pipeline/api/workbench_routes.py`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: `syncLazyEditor({ open, mounted, mount, unmount }) -> mounted | null`
- `mount()` 只在 `open === true && mounted === null` 时调用。
- `unmount(mounted)` 只在 `open === false && mounted !== null` 时调用。

- [ ] **Step 1: 写失败的 Node 单元测试与静态资源合同测试**

测试依次验证关闭状态不挂载、首次展开只挂载一次、重复同步不重复挂载、收起卸载并返回 `null`；合同测试请求 `/ui/director-lazy-editor.js` 并断言 200。

- [ ] **Step 2: 运行测试并确认因模块或静态路由缺失而失败**

Run: `uv run --extra dev pytest tests/unit/test_director_lazy_editor_js.py tests/contract/test_workbench_api.py::test_workbench_serves_local_static_shell_and_public_chapter_listing -q`

Expected: FAIL，原因是模块文件不存在或 URL 返回 404。

- [ ] **Step 3: 实现最小辅助函数并加入静态白名单**

```js
export function syncLazyEditor({ open, mounted, mount, unmount }) {
  if (open && mounted === null) return mount();
  if (!open && mounted !== null) {
    unmount(mounted);
    return null;
  }
  return mounted;
}
```

- [ ] **Step 4: 运行目标测试并确认通过**

Run: `uv run pytest tests/unit/test_director_lazy_editor_js.py tests/contract/test_workbench_api.py::test_workbench_serves_local_static_shell_and_public_chapter_listing -q`

Expected: `2 passed`。

- [ ] **Step 5: 提交**

```bash
git add src/voice_pipeline/webui/director-lazy-editor.js src/voice_pipeline/api/workbench_routes.py tests/unit/test_director_lazy_editor_js.py tests/contract/test_workbench_api.py
git commit -m "test: cover lazy director translation editor"
```

### Task 2: 折叠面板与延迟表单创建

**Files:**
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: `syncLazyEditor({ open, mounted, mount, unmount })` from `./director-lazy-editor.js`.
- Produces: `lazyTranslatedEditor(utterance) -> HTMLDetailsElement`，默认关闭且不含 `.director-translation-editor`。

- [ ] **Step 1: 扩展合同测试，要求主脚本导入懒挂载模块并使用折叠入口**

断言 `director.js` 导入 `./director-lazy-editor.js`，包含“编辑译文与情绪”和 `details.ontoggle`，且渲染路径调用 `lazyTranslatedEditor(utterance)` 而不是直接调用 `translatedEditor(utterance)`。

- [ ] **Step 2: 运行合同测试并确认失败**

Run: `uv run pytest tests/contract/test_workbench_api.py::test_workbench_serves_local_static_shell_and_public_chapter_listing -q`

Expected: FAIL，原因是导演脚本尚未提供懒加载折叠面板。

- [ ] **Step 3: 实现折叠容器和按需挂载**

`lazyTranslatedEditor()` 创建关闭的 `<details>` 与 `<summary>`；`ontoggle` 使用 `syncLazyEditor`，展开时追加 `translatedEditor(utterance)`，收起时调用 `remove()`。摘要根据 `dirtyTranslations.has(utterance_id)` 显示未保存状态。

- [ ] **Step 4: 增加紧凑样式**

让 `.director-translation-details` 横跨卡片全部列，为摘要提供按钮式焦点与 hover 样式，展开表单只增加必要间距。

- [ ] **Step 5: 运行目标测试和 JavaScript 语法检查**

Run: `uv run pytest tests/unit/test_director_lazy_editor_js.py tests/contract/test_workbench_api.py::test_workbench_serves_local_static_shell_and_public_chapter_listing -q`

Run: `node --check src/voice_pipeline/webui/director.js && node --check src/voice_pipeline/webui/director-lazy-editor.js`

Expected: 测试通过，两个语法检查退出码均为 0。

- [ ] **Step 6: 提交**

```bash
git add src/voice_pipeline/webui/director.js src/voice_pipeline/webui/styles.css tests/contract/test_workbench_api.py
git commit -m "fix: lazy load director translation editors"
```

### Task 3: 全量验证、部署与实际 WebUI 验收

**Files:**
- Verify only.

**Interfaces:**
- Consumes: 合并后的 wheel 与 `D:\TTSsystem\config\app.example.yaml`。
- Produces: 运行中的 `http://127.0.0.1:8765/`。

- [ ] **Step 1: 运行静态检查与全量非 GPU 测试**

Run: `uv run ruff check src tests`

Run: `uv run mypy src workers`

Run: `uv run pytest -q -m "not gpu and not gpu_residency and not quality_model"`

Expected: 全部退出码 0。

- [ ] **Step 2: 构建 wheel 并验证新模块存在**

Run: `$out = Join-Path $env:TEMP ('voice-pipeline-lazy-editor-' + [guid]::NewGuid().ToString('N')); uv build --wheel --out-dir $out`，随后检查 wheel 同时包含 `director.js` 与 `director-lazy-editor.js`。

- [ ] **Step 3: 推送 PR、等待 CI、合并并重新部署**

使用用户配置的 GitHub 身份提交；CI 通过后合并到 `main`，再安装合并 SHA 构建的 wheel 并重启服务。

- [ ] **Step 4: 浏览器验收懒挂载行为**

在翻译校对项目中验证：初始 `.director-translation-editor` 数量为 0；展开一个“编辑译文与情绪”后数量为 1；收起后回到 0；浏览器控制台无 error。

- [ ] **Step 5: 最终运行状态检查**

确认 `/api/v1/health` 为 `ready`、新模块 URL 为 200、队列无运行任务、工作树干净且 `HEAD == origin/main`。
