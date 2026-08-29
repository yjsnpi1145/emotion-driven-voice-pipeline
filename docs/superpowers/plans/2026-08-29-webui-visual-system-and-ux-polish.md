# WebUI Visual System and UX Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Apply a coherent, accessible dark production-console visual system across every existing WebUI page without changing business behavior, state machines, or user content.

**Architecture:** Keep the existing FastAPI-served semantic HTML, native JavaScript modules, and single CSS bundle. Add static design-contract tests first, then introduce compatible CSS tokens, local inline SVG navigation icons, page-level component polish, responsive rules, and cache-version updates. JavaScript changes are limited to module cache query strings; event handling and state transitions remain untouched.

**Tech Stack:** FastAPI static assets, semantic HTML5, native ES modules, CSS Grid/Flexbox/custom properties, pytest, Node syntax checks, in-app browser acceptance testing.

## Global Constraints

- Keep the native FastAPI SPA; do not add React, Vue, Tailwind, a Node build chain, a CDN, a font download, an icon package, or any new runtime dependency.
- Do not change APIs, persisted formats, task state machines, SSE lifecycles, concurrency protection, form names, dynamic element IDs, or button enablement rules.
- Do not change source text, roles, translations, Chinese reference text, or any user content.
- Global performance direction may change only emotion vectors, speed, and pauses.
- Preserve recomposition, local regeneration, per-sentence export, and historical partially successful project compatibility.
- Do not read or expose real API keys, runtime data, user articles/audio, models, or external weights.
- Retain full-width desktop productivity layouts and verify 375px, 768px, 1024px, and 1440px widths without unintended horizontal overflow.
- Use local inline SVG with accessible names or aria-hidden="true"; do not use emoji or Unicode symbols as structural navigation icons.
- After changing src, build the wheel, force-install it into .venv-control, restart the real service, then perform browser acceptance.
- Rollback baseline: codex/backup-ui-ux-6f5cdf2 at commit 6f5cdf2; do not reset, discard, or overwrite existing local commits.

## File Map

- Create tests/unit/test_webui_visual_system.py for visual-system, icon, component, responsive, and accessibility contracts.
- Modify tests/contract/test_workbench_api.py only for new shell/cache expectations.
- Modify tests/integration_cpu/test_webui_workbench.py only for the Director module cache-version expectation.
- Modify src/voice_pipeline/webui/index.html for the SVG symbol sprite, shell icons, and entry-asset versions.
- Modify src/voice_pipeline/webui/styles.css for tokens and all visual/responsive work.
- Modify src/voice_pipeline/webui/director.js only for local module query strings.
- Generate dist/emotion_driven_voice_pipeline-0.1.0-py3-none-any.whl from the verified source tree.

---

### Task 1: Global design tokens and accessibility foundation

**Files:**
- Create: tests/unit/test_webui_visual_system.py
- Modify: src/voice_pipeline/webui/styles.css:1-49

**Interfaces:**
- Consumes: existing root variables such as --bg, --panel, --text, and --accent.
- Produces: semantic custom properties used by later tasks while keeping old variables as aliases.

- [ ] **Step 1: Write the failing token and accessibility tests**

Create tests/unit/test_webui_visual_system.py:

~~~python
from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).parents[2] / "src" / "voice_pipeline" / "webui"


def _asset(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def _rule(stylesheet: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)
    assert match is not None, f"missing CSS rule: {selector}"
    return match.group(1)


def test_global_design_tokens_and_focus_contract() -> None:
    stylesheet = _asset("styles.css")
    for token in (
        "--surface-canvas:", "--surface-panel:", "--surface-subtle:",
        "--surface-raised:", "--text-primary:", "--text-secondary:",
        "--text-tertiary:", "--interactive:", "--focus-ring:",
        "--space-1:", "--space-6:", "--text-xs:", "--text-xl:",
        "--radius-md:", "--control-height:", "--shadow-panel:",
    ):
        assert token in stylesheet
    assert "scroll-padding-top: var(--shell-offset)" in stylesheet
    focus_rule = _rule(
        stylesheet,
        "button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible, summary:focus-visible",
    )
    assert "var(--focus-ring)" in focus_rule
    assert "outline-offset: 2px" in focus_rule


def test_global_controls_use_shared_height_and_motion_tokens() -> None:
    stylesheet = _asset("styles.css")
    button_rule = _rule(stylesheet, "button")
    field_rule = _rule(stylesheet, "input, textarea, select")
    assert "min-height: var(--control-height)" in button_rule
    assert "var(--radius-control)" in button_rule
    assert "var(--motion-fast)" in button_rule
    assert "min-height: var(--control-height)" in field_rule
    assert "var(--radius-control)" in field_rule
~~~

- [ ] **Step 2: Run the tests and verify RED**

Run:

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py -q
~~~

Expected: both tests fail because the semantic tokens and token-based control declarations do not exist.

- [ ] **Step 3: Implement tokens and global controls**

Add exact semantic families to :root: surfaces, three text levels, interactive/focus colors, 4/8/12/16/20/24/32px spacing, 12/14/16/20/28px type, 6/8/10/12px radii, 36/40/44px control sizes, motion, shadows, and shell offset. Map the existing variables to the new tokens:

~~~css
--bg: var(--surface-canvas);
--panel: var(--surface-panel);
--panel-soft: var(--surface-subtle);
--panel-raised: var(--surface-raised);
--input: var(--surface-input);
--line: var(--border-default);
--line-strong: var(--border-strong);
--text: var(--text-primary);
--muted: var(--text-tertiary);
--accent: var(--interactive);
--accent-hover: var(--interactive-hover);
--accent-soft: var(--interactive-soft);
--shadow: var(--shadow-panel);
~~~

Set html scroll-padding-top to var(--shell-offset), body line-height to 1.5, controls to shared heights/radii/transitions, and focus-visible to a two-pixel var(--focus-ring) outline with two-pixel offset.

- [ ] **Step 4: Run focused tests**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py tests/contract/test_workbench_api.py -q
~~~

Expected: new tests and existing contracts pass.

- [ ] **Step 5: Commit**

~~~powershell
git add tests/unit/test_webui_visual_system.py src/voice_pipeline/webui/styles.css
git commit -m "style: establish WebUI design tokens"
~~~

---

### Task 2: Accessible local icons and application shell

**Files:**
- Modify: tests/unit/test_webui_visual_system.py
- Modify: tests/contract/test_workbench_api.py:49-50,198-216
- Modify: tests/integration_cpu/test_webui_workbench.py:26
- Modify: src/voice_pipeline/webui/index.html:1-38,373-374
- Modify: src/voice_pipeline/webui/director.js:1-31
- Modify: src/voice_pipeline/webui/styles.css:50-100,374-447

**Interfaces:**
- Consumes: tab buttons with data-view and aria-selected, refresh-global, and existing switch logic.
- Produces: local SVG symbols and tab/icon instances without changing IDs or event bindings.

- [ ] **Step 1: Add a failing icon/cache contract**

Append:

~~~python
def test_navigation_uses_local_accessible_svg_icons_and_new_cache_version() -> None:
    page = _asset("index.html")
    director_script = _asset("director.js")
    assert 'class="icon-sprite" aria-hidden="true"' in page
    assert page.count('class="tab-icon" aria-hidden="true"') == 5
    assert 'class="button-icon" aria-hidden="true"' in page
    for glyph in ("◉", "◎", "◆", "✦", "▦", "↻"):
        assert glyph not in page
    assert 'href="/ui/styles.css?v=20260829a"' in page
    assert 'src="/ui/app.js?v=20260829a"' in page
    assert 'src="/ui/director.js?v=20260829a"' in page
    assert 'from "./director-dnd.js?v=20260829a"' in director_script
    assert 'from "./director-adjustment.js?v=20260829a"' in director_script
~~~

- [ ] **Step 2: Run and verify RED**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py::test_navigation_uses_local_accessible_svg_icons_and_new_cache_version -q
~~~

Expected: failure because Unicode glyphs and version 20260828a remain.

- [ ] **Step 3: Implement the shell icon system**

Insert a zero-sized SVG sprite directly inside body with six outline symbols: icon-waveform, icon-director, icon-model, icon-spark, icon-system, and icon-refresh. Use local path/circle/rect/polyline elements only.

Every tab retains exact attributes and visible text:

~~~html
<button class="tab-button" type="button" data-view="workbench" aria-selected="true">
  <svg class="tab-icon" aria-hidden="true"><use href="#icon-waveform"></use></svg>
  <span class="tab-label">配音工作台</span>
</button>
~~~

Refresh retains its ID/title/aria-label:

~~~html
<button id="refresh-global" class="icon-button" type="button" title="刷新当前页面" aria-label="刷新当前页面">
  <svg class="button-icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>
</button>
~~~

Update index entry versions and every existing Director dependency query string to 20260829a. Update only corresponding assertions in the two Python tests.

- [ ] **Step 4: Style and verify the shell**

Add .icon-sprite, .tab-icon, .button-icon, and .tab-label. Keep sticky behavior, use semantic tokens, make the icon button 40px, provide an active indicator not dependent only on color, and retain narrow-screen horizontal navigation.

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py::test_director_preprocessing_review_assets_are_wired -q
node --check src/voice_pipeline/webui/director.js
~~~

Expected: all tests pass and syntax check exits zero.

- [ ] **Step 5: Commit**

~~~powershell
git add tests/unit/test_webui_visual_system.py tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py src/voice_pipeline/webui/index.html src/voice_pipeline/webui/director.js src/voice_pipeline/webui/styles.css
git commit -m "style: refine WebUI navigation shell"
~~~

---

### Task 3: Shared components and ordinary workbench hierarchy

**Files:**
- Modify: tests/unit/test_webui_visual_system.py
- Modify: src/voice_pipeline/webui/styles.css:76-245

**Interfaces:**
- Consumes: Task 1 tokens and existing workbench classes rendered by index.html/app.js.
- Produces: consistent panels, headings, controls, chips, summaries, list rows, scroll regions, and workbench breakpoints.

- [ ] **Step 1: Add failing component contracts**

Append:

~~~python
def test_shared_panels_and_workbench_use_semantic_component_tokens() -> None:
    stylesheet = _asset("styles.css")
    panel_rule = _rule(stylesheet, ".panel")
    toolbar_rule = _rule(stylesheet, ".chapter-toolbar")
    selected_rule = _rule(stylesheet, '.segment-row[data-selected="true"]')
    scroll_rule = _rule(stylesheet, ".workbench > .panel")
    assert "var(--radius-lg)" in panel_rule
    assert "var(--shadow-panel)" in panel_rule
    assert "var(--radius-lg)" in toolbar_rule
    assert "var(--space-4)" in toolbar_rule
    assert "inset 3px 0 var(--interactive)" in selected_rule
    assert "scrollbar-gutter: stable" in scroll_rule


def test_workbench_keeps_three_two_one_column_progression() -> None:
    stylesheet = _asset("styles.css")
    assert re.search(
        r"\.workbench\s*\{[^}]*grid-template-columns:\s*minmax\(17rem,\s*\.82fr\)\s+minmax\(20rem,\s*1fr\)\s+minmax\(30rem,\s*1\.65fr\)",
        stylesheet,
    )
    assert "@media (max-width: 1220px)" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".chapter-action-buttons" in stylesheet
~~~

- [ ] **Step 2: Run and verify RED**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py::test_shared_panels_and_workbench_use_semantic_component_tokens tests/unit/test_webui_visual_system.py::test_workbench_keeps_three_two_one_column_progression -q
~~~

Expected: tokenized component test fails; the layout guard remains green.

- [ ] **Step 3: Apply shared component polish**

Use these exact core declarations while preserving all selectors and virtual-row dimensions:

~~~css
.panel {
  min-width: 0;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--surface-panel);
  box-shadow: var(--shadow-panel);
}

.workbench > .panel {
  min-height: 0;
  overflow: auto;
  scrollbar-gutter: stable;
}

.segment-row[data-selected="true"] {
  border-color: var(--interactive);
  background: var(--interactive-soft);
  box-shadow: inset 3px 0 var(--interactive);
}
~~~

Tokenize chapter toolbar/summary/actions, accordion, activity console, stage progress, chapter rows, segment filters/list, editor, chips, forms, help text, hover, selected, disabled, warning, and error states. Preserve the 96px segment-row height and the exact desktop three-column declaration.

- [ ] **Step 4: Verify**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py -q
node --check src/voice_pipeline/webui/app.js
~~~

Expected: all tests pass and syntax check exits zero.

- [ ] **Step 5: Commit**

~~~powershell
git add tests/unit/test_webui_visual_system.py src/voice_pipeline/webui/styles.css
git commit -m "style: clarify workbench hierarchy"
~~~

---

### Task 4: Director, settings, system, dialog, and responsive polish

**Files:**
- Modify: tests/unit/test_webui_visual_system.py
- Modify: src/voice_pipeline/webui/styles.css:246-447

**Interfaces:**
- Consumes: existing Director/settings/system/dialog selectors and Task 1 tokens.
- Produces: consistent cross-page surfaces, action wrapping, dialog layout, mobile touch targets, and reduced-motion behavior.

- [ ] **Step 1: Add failing cross-page contracts**

Append:

~~~python
def test_director_settings_system_and_dialog_share_visual_language() -> None:
    stylesheet = _asset("styles.css")
    for selector in (
        ".director-workspace", ".settings-card", ".health-card",
        ".director-adjustment-dialog",
    ):
        rule = _rule(stylesheet, selector)
        assert "var(--radius-lg)" in rule or "var(--radius-md)" in rule
    assert "flex-wrap: wrap" in _rule(stylesheet, ".director-actions")
    assert "min-width: 0" in _rule(stylesheet, ".director-heading-actions")


def test_mobile_touch_targets_and_reduced_motion_cover_feedback() -> None:
    stylesheet = _asset("styles.css")
    mobile = stylesheet.split("@media (max-width: 560px)", 1)[1]
    reduced = stylesheet.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "min-height: var(--touch-target)" in mobile
    assert ".tab-button" in mobile
    assert ".toast" in reduced
    assert "transition-duration: 0.01ms" in reduced
~~~

- [ ] **Step 2: Run and verify RED**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py::test_director_settings_system_and_dialog_share_visual_language tests/unit/test_webui_visual_system.py::test_mobile_touch_targets_and_reduced_motion_cover_feedback -q
~~~

Expected: both tests fail because the cross-page and reduced-motion contracts are absent.

- [ ] **Step 3: Apply cross-page polish**

Tokenize and align existing selectors for page headings, Director layout/project/workspace/preset panels, stage rail, role/timeline zones, utterance cards, action groups, settings cards, model cards, health cards, folder actions, and the adjustment dialog. Add min-width: 0 to flexible children and flex-wrap: wrap to action groups.

Keep these invariants unchanged:

~~~text
Director desktop columns remain project / workspace / preset.
Director stage order and data-state attributes remain unchanged.
Utterance drag/drop and selection selectors remain unchanged.
Adjustment dialog ID and data-adjustment-action values remain unchanged.
Health stopped_expected interpretation remains unchanged.
LLM API-key DOM behavior remains unchanged.
~~~

At 820px retain single-column page layouts. At 560px apply min-height: var(--touch-target) to tabs and action buttons, wrap action groups, and keep the dialog within 0.5rem of the viewport.

Use this reduced-motion contract:

~~~css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }

  .toast,
  .stage-progress-segment[data-state="active"]::after,
  .llm-activity-status[data-state="active"]::before {
    animation: none;
  }
}
~~~

- [ ] **Step 4: Verify focused UI behavior**

~~~powershell
.venv-control\Scripts\python.exe -m pytest tests/unit/test_webui_visual_system.py tests/unit/test_director_adjustment_js.py tests/unit/test_director_dnd_js.py tests/unit/test_director_llm_activity_js.py tests/unit/test_director_preprocessing_js.py tests/unit/test_director_reference_pool_js.py tests/unit/test_director_working_text_js.py tests/integration_cpu/test_webui_workbench.py tests/contract/test_workbench_api.py -q
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/director.js
node --check src/voice_pipeline/webui/director-adjustment.js
~~~

Expected: all selected tests pass and all syntax checks exit zero.

- [ ] **Step 5: Commit**

~~~powershell
git add tests/unit/test_webui_visual_system.py src/voice_pipeline/webui/styles.css
git commit -m "style: unify Director and settings surfaces"
~~~

---

### Task 5: Full verification, deployment, and browser acceptance

**Files:**
- Verify: all tracked files changed by Tasks 1-4
- Generate: dist/emotion_driven_voice_pipeline-0.1.0-py3-none-any.whl
- Inspect: runtime/logs operational summaries only

**Interfaces:**
- Consumes: completed source and scripts/start.ps1.
- Produces: force-installed real service and browser evidence at all required widths.

- [ ] **Step 1: Run static checks**

~~~powershell
git diff --check
.venv-control\Scripts\python.exe -m ruff check src tests
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/director.js
node --check src/voice_pipeline/webui/director-adjustment.js
~~~

Expected: every command exits zero.

- [ ] **Step 2: Run the full non-GPU suite**

~~~powershell
.venv\Scripts\python.exe -m pytest -m "not gpu and not quality_model and not process and not crash_recovery" -q
~~~

Expected: zero failures; GPU, pinned-quality-model, and subprocess lifecycle tests are deselected because the real service remains active during this run.

- [ ] **Step 3: Build and force-install**

~~~powershell
Remove-Item -LiteralPath dist\emotion_driven_voice_pipeline-0.1.0-py3-none-any.whl -ErrorAction SilentlyContinue
.venv-control\Scripts\python.exe -m build --wheel
uv pip install --python .venv-control\Scripts\python.exe --force-reinstall --no-deps dist\emotion_driven_voice_pipeline-0.1.0-py3-none-any.whl
~~~

Expected: build and install exit zero. If the venv has no build module, use the repository's already established wheel-build command without adding a dependency.

- [ ] **Step 4: Restart and verify the real service**

Use scripts/start.ps1 with config/app.example.yaml and the established real-service options, then request:

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/health
~~~

Expected: ready, real mode, migration 0009_director_performance_controls, quick check successful, and idle workers may be stopped_expected. Inspect only recent operational summaries; do not print prompts, user text, keys, audio paths, or database contents.

- [ ] **Step 5: Perform browser acceptance**

Reload http://127.0.0.1:8765/. At 375, 768, 1024, and 1440px:

~~~text
Confirm document scroll width does not exceed viewport width.
Visit all five tabs without submitting or mutating data.
Confirm selected tabs have visible text, local SVG icons, and a non-color-only indicator.
Confirm headings, panels, forms, chips, lists, and action groups remain readable.
Confirm keyboard focus is visible and not obscured by the sticky shell.
Open an existing sentence-adjustment dialog only through a non-mutating view action, inspect it, and close without saving or regeneration.
Confirm the browser console has zero new errors.
~~~

Do not create projects, submit forms, regenerate audio, alter settings, or reveal private project text during acceptance.

- [ ] **Step 6: Update design status and commit evidence**

Change the design status from 待用户评审 to 已实施, then run:

~~~powershell
git diff --check
git status -sb
git diff --stat codex/backup-ui-ux-6f5cdf2..HEAD
git add docs/superpowers/specs/2026-08-29-webui-visual-system-and-ux-polish-design.md
git commit -m "docs: record WebUI polish completion"
~~~

Expected: final working tree clean, main ahead of origin/main, and no push or pull request.

