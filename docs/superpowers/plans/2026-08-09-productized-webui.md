# Productized Local WebUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a GPT-SoVITS-inspired, productized local control console with native file/folder actions, live OpenAI-compatible LLM settings, model management, and system diagnostics.

**Architecture:** Keep the existing FastAPI and vanilla SPA boundary. Add a constrained Windows desktop bridge and a hot-swappable RuntimeDirector; expose both through `/api/v1`, then reorganize the SPA into four persistent tab views while preserving all existing workbench behavior.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, httpx, tkinter, vanilla ES modules, CSS, pytest.

## Global Constraints

- Bind only to `127.0.0.1` and keep every endpoint under `/api/v1`.
- Never accept arbitrary shell commands or arbitrary open-folder paths.
- Never return or render the LLM API key.
- Persist runtime settings only below `runtime/state` using atomic replacement.
- Keep the single GPU consumer and existing immutable version semantics unchanged.
- Use no external CDN and add no frontend build tool.

---

### Task 1: Runtime LLM settings and hot-swappable director

**Files:**
- Create: `src/voice_pipeline/models/runtime_settings.py`
- Create: `src/voice_pipeline/modules/llm/runtime.py`
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/core/chapter_service.py`
- Test: `tests/unit/test_runtime_llm_settings.py`

**Interfaces:**
- Produces: `RuntimeDirector.from_config(settings, state_dir)`, `view()`, `update(request)`, `test(request)`, `create_plan(...)`, `correct_reference_text(...)`, `max_reference_corrections`.
- Consumes: existing `LlmSettings`, `FakeDirector`, and `OpenAiDirectorClient`.

- [ ] Write tests proving settings restore, write-only key handling, atomic persistence, hot update, and connection-test non-persistence.
- [ ] Run `python -m pytest tests/unit/test_runtime_llm_settings.py -q` and confirm failures are caused by missing models/runtime.
- [ ] Implement strict request/view models and RuntimeDirector with one async lock and atomic settings files.
- [ ] Add explicit API-key injection and minimal connection test to `OpenAiDirectorClient`.
- [ ] Change ChapterService to read `director.max_reference_corrections` at correction time.
- [ ] Run the unit test and existing LLM/chapter tests; expect all pass.

### Task 2: Constrained desktop bridge and API routes

**Files:**
- Create: `src/voice_pipeline/core/desktop_service.py`
- Create: `src/voice_pipeline/api/product_routes.py`
- Modify: `src/voice_pipeline/core/model_profile_service.py`
- Modify: `src/voice_pipeline/api/app.py`
- Test: `tests/contract/test_product_settings_api.py`
- Test: `tests/unit/test_desktop_service.py`

**Interfaces:**
- Produces: `DesktopService.paths()`, `open_resource(key)`, `open_profile(profile_id)`, `pick_file(kind)` and the LLM/local REST routes.
- Consumes: configured model root, allowed import roots, artifact root, runtime logs path, and model profile store resolution.

- [ ] Write contract and unit tests for exact endpoint schemas, path containment, extension filters, cancellation, missing folders, and unknown enum values.
- [ ] Run the new tests and confirm expected 404/import failures.
- [ ] Implement the desktop service with injected opener/picker functions so tests never open a real window.
- [ ] Implement product routes and attach RuntimeDirector/DesktopService during FastAPI lifespan.
- [ ] Run product API, model-profile, and app contract tests; expect all pass.

### Task 3: Product shell and model/LLM/system pages

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Test: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: Task 2 `/local/*`, `/settings/llm*`, existing model profile and health endpoints.
- Produces: four tab panels, native browse/open actions, LLM editor, health dashboard, current model indicator, and shared toast/busy helpers.

- [ ] Extend the static shell contract with four navigation buttons, model/library controls, LLM fields, system cards, toast region, and no-CDN assertions.
- [ ] Run the contract test and confirm it fails because the new shell markers are absent.
- [ ] Rebuild index markup using persistent tab panels; keep all existing element IDs required by workbench logic.
- [ ] Add JS page routing, profile cards, native file picking, folder opening, LLM load/test/save, health rendering, and sanitized toasts.
- [ ] Restyle with GPT-SoVITS-like tabs, accordions, compact field rows and primary buttons; add desktop and narrow-screen breakpoints.
- [ ] Run `node --check` and static contract tests; expect pass.

### Task 4: Regression, packaging, deployment and browser acceptance

**Files:**
- Modify only if verification exposes a tested defect.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: installed control-plane wheel and running `http://127.0.0.1:8765/` product console.

- [ ] Run compileall, Ruff format/check, mypy, node syntax, and all non-GPU tests.
- [ ] Build the wheel and assert it contains every WebUI asset and new backend module.
- [ ] Stop the current control plane, run `scripts/setup-control.ps1`, then start `config/acceptance.gpu.local.yaml`.
- [ ] Verify health, root HTML, JS markers, LLM settings GET, and local paths GET over HTTP.
- [ ] In the in-app browser, verify all four tabs, no console errors, responsive landmarks, LLM key non-disclosure, and model open-folder buttons.
- [ ] Commit the implementation after fresh evidence is green.

