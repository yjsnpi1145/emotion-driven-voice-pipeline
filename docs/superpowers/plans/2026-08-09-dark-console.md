# Dark Console Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除“声”Logo，并把完整 WebUI 改为固定深色控制台主题。

**Architecture:** 仅调整 HTML 品牌标记与 CSS 视觉令牌/组件表面；保持 JavaScript、API、布局与数据流不变。通过静态契约与真实浏览器计算样式验证主题完整性。

**Tech Stack:** HTML5、CSS3、pytest、Codex in-app browser。

## Global Constraints

- 固定深色主题，不新增主题切换和本地存储。
- HTML 根节点必须标记 `data-theme="dark-console"`。
- 删除 `.brand-mark` 元素和样式。
- 保留现有响应式断点、音频 metadata 预加载与所有业务交互。
- 新行为必须先写测试并观察预期失败。

---

### Task 1: Dark-theme contract

**Files:**
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: served `/` and `/ui/styles.css`.
- Produces: stable contract for logo removal and dark surfaces.

- [ ] **Step 1: Write failing assertions**

```python
assert 'data-theme="dark-console"' in page.text
assert 'class="brand-mark"' not in page.text
assert ".brand-mark" not in stylesheet.text
assert "color-scheme: dark" in stylesheet.text
assert "--bg: #080d15" in stylesheet.text
assert "background: #fff" not in stylesheet.text
```

Also assert cache token advances to `20260809h`.

- [ ] **Step 2: Verify RED**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/contract/test_workbench_api.py
```

Expected: FAIL on the existing light theme and Logo.

### Task 2: Header and complete dark surface pass

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/styles.css`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Produces: fixed `dark-console` theme without changing DOM ids consumed by JavaScript.

- [ ] **Step 1: Remove the Logo and mark theme**

```html
<html lang="zh-CN" data-theme="dark-console">
...
<div class="brand-lockup">
  <div>
    <p class="eyebrow">GPT-SoVITS × IndexTTS2</p>
    <h1>情绪配音工作台</h1>
  </div>
</div>
```

Advance both stylesheet and app cache query strings to `20260809h`.

- [ ] **Step 2: Replace global color tokens**

Use deep navy/charcoal backgrounds, high-contrast text, blue accents, and dark success/warning/danger surfaces. Set `color-scheme: dark`.

- [ ] **Step 3: Replace hard-coded light component colors**

Cover inputs, top bars, tabs, toolbars, rows, nested cards, audio/history cards, progress stages, model cards, health cards, Toasts and state-specific backgrounds. Remove `.brand-mark` styling entirely.

- [ ] **Step 4: Verify GREEN and static quality**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/contract/test_workbench_api.py
uv run --python .\.venv-control\Scripts\python.exe ruff check src tests
uv run --python .\.venv-control\Scripts\python.exe mypy src
node --check src/voice_pipeline/webui/app.js
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add src/voice_pipeline/webui/index.html src/voice_pipeline/webui/styles.css tests/contract/test_workbench_api.py
git commit -m "feat: apply dark console theme"
```

### Task 3: Regression, visual QA and deployment

**Files:**
- No production files expected.

- [ ] **Step 1: Full non-GPU suite**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q -m "not gpu and not gpu_residency and not quality_model"
```

- [ ] **Step 2: Build and deploy committed wheel**

Build under `runtime/build/<commit>/`, force-install into `.venv-control`, restart with `config/acceptance.gpu.local.yaml` and require health `ready`.

- [ ] **Step 3: Real-browser visual verification**

Reload `http://127.0.0.1:8765/`; verify `.brand-mark` count is zero, computed body background is dark, top bar/panel/input backgrounds are dark, text contrast is readable, stage progress does not overflow, and console has no warnings/errors.

- [ ] **Step 4: Final checks**

Confirm 8765 service is ready, dispatcher is running, migration remains `0003_chapter_history_soft_delete`, and Git is clean.
