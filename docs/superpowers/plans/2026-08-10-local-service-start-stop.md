# Local Service Start and Shutdown Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a root-level Windows launcher that starts the real local stack and opens the WebUI, plus a WebUI control that shuts down the control plane and every managed model process.

**Architecture:** The BAT file is a thin wrapper around the existing `scripts/start.ps1`; it never launches model workers directly. A small browser-independent ES module owns shutdown confirmation and HTTP semantics, while `app.js` owns DOM state, poller cleanup, and the final offline overlay. The existing `/api/v1/control/shutdown` backend contract remains the single shutdown authority.

**Tech Stack:** Windows CMD, PowerShell 7, vanilla ES modules, FastAPI, pytest, Node.js.

## Global Constraints

- The default launcher uses `config/app.example.yaml` and `.venv-control\Scripts\python.exe`.
- The launcher resolves the repository from `%~dp0`; it must not contain a machine-specific absolute path.
- `VOICE_PIPELINE_NO_BROWSER=1` skips only browser launch; `VOICE_PIPELINE_NO_PAUSE=1` skips only error pausing.
- The WebUI must confirm before shutdown and must send exactly one `POST /api/v1/control/shutdown` request.
- A connection drop after the shutdown attempt is a successful terminal outcome; an explicit non-2xx HTTP response is an error.
- The terminal UI stops chapter polling, SSE, LLM activity polling, and disables all interactive controls.
- Do not add another backend shutdown endpoint or directly manage model PIDs from JavaScript or BAT.

---

### Task 1: Root Windows Launcher

**Files:**
- Create: `启动服务.bat`
- Modify: `tests/repository/test_public_release_policy.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `scripts/start.ps1 -Config <path> -PythonExecutable <path> -Json` and `GET /api/v1/health`.
- Produces: a double-click launcher with exit code `0` on ready/opened and nonzero on prerequisites or startup failure.

- [ ] **Step 1: Write the failing launcher contract tests**

Add tests that decode `启动服务.bat` as UTF-8 and assert it contains `%~dp0`, `pwsh`,
`scripts\start.ps1`, `config\app.example.yaml`, `.venv-control\Scripts\python.exe`, the health URL,
`VOICE_PIPELINE_NO_BROWSER`, and `start "" "http://127.0.0.1:8765/"`. Assert it contains neither
`D:\TTSsystem` nor direct IndexTTS2/GPT-SoVITS worker commands.

- [ ] **Step 2: Run the launcher tests and verify RED**

Run:

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest tests/repository/test_public_release_policy.py -q
```

Expected: FAIL because `启动服务.bat` does not exist.

- [ ] **Step 3: Implement the minimal BAT wrapper**

The BAT must `pushd "%~dp0"`, validate `pwsh`, the config, and control Python, probe health with
PowerShell, call `scripts\start.ps1` only when not ready, and open the browser unless
`VOICE_PIPELINE_NO_BROWSER=1`. All failure labels return a nonzero exit code and pause unless
`VOICE_PIPELINE_NO_PAUSE=1`.

- [ ] **Step 4: Document the double-click path and rerun tests**

Add a short README “Windows 一键启动” section, then rerun the Task 1 command. Expected: all
repository release-policy tests PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- "启动服务.bat" README.md tests/repository/test_public_release_policy.py
git commit -m "feat: add windows service launcher"
```

### Task 2: Testable Browser Shutdown Coordinator

**Files:**
- Create: `src/voice_pipeline/webui/service-shutdown.js`
- Create: `tests/unit/test_service_shutdown_js.py`

**Interfaces:**
- Produces: `confirmAndShutdown({ confirmShutdown, fetchImpl, onStarting, onComplete }) -> Promise<{status: string}>`.
- `status` is exactly one of `cancelled`, `accepted`, or `disconnected`.

- [ ] **Step 1: Write the failing Node-backed unit test**

Use Python to execute Node with an inline ES module that imports `confirmAndShutdown` and asserts:

1. confirmation `false` makes zero fetch calls and returns `cancelled`;
2. HTTP 200 calls `onStarting`, then `onComplete`, and returns `accepted`;
3. rejected fetch calls `onComplete` and returns `disconnected`;
4. HTTP 500 rejects and does not call `onComplete`.

- [ ] **Step 2: Run the unit test and verify RED**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest tests/unit/test_service_shutdown_js.py -q
```

Expected: FAIL because `service-shutdown.js` does not exist.

- [ ] **Step 3: Implement the coordinator**

Export `confirmAndShutdown`. Call the injected confirmation first; after approval call `onStarting`,
then issue one fetch with `{method: "POST"}`. Treat thrown fetch errors as disconnection success. For
non-2xx responses, read the response body and throw an `Error` containing the HTTP status.

- [ ] **Step 4: Rerun the unit test**

Run the Task 2 test command. Expected: all shutdown module scenarios PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/voice_pipeline/webui/service-shutdown.js tests/unit/test_service_shutdown_js.py
git commit -m "feat: add web shutdown coordinator"
```

### Task 3: WebUI Shutdown Button and Offline State

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: `confirmAndShutdown` from Task 2 and `POST /api/v1/control/shutdown`.
- Produces: DOM elements `#shutdown-services` and `#shutdown-overlay`, plus `enterShutdownState()` in `app.js`.

- [ ] **Step 1: Extend the WebUI contract test and verify RED**

Assert the served HTML contains both element IDs and shutdown copy, the script imports
`./service-shutdown.js`, uses the shutdown endpoint, closes `state.events`, clears both polling timers,
and disables interactive controls. Assert CSS contains `.danger-button` and `.shutdown-overlay`.

Run:

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest tests/contract/test_workbench_api.py -q
```

Expected: FAIL because the button, overlay, module import, and styles are absent.

- [ ] **Step 2: Add the semantic UI shell**

Place the danger button in `.appbar-status`. Add a hidden, assertive live-region overlay after the app
shell with the final text “所有服务已关闭，可以关闭此页面”. Bump static asset query versions so an
already-running browser does not reuse old JavaScript or CSS.

- [ ] **Step 3: Wire shutdown and terminal state**

Import `confirmAndShutdown`. Implement `stopClientActivity()` to close SSE and clear
`refreshTimer`, `llmActivityTimer`, and `toastTimer`. Implement `enterShutdownState()` to call that
function, disable every `button`, `input`, `textarea`, and `select`, then reveal the overlay. Bind the
button to a confirmation message that warns active generation will be interrupted. Restore the button
only for explicit HTTP errors.

- [ ] **Step 4: Add dark-console danger and overlay styles**

Use existing `--danger`, `--danger-soft`, `--panel`, and `--shadow` tokens. The overlay must cover the
viewport above sticky navigation, remain readable at 320px, and avoid animations that imply the server
is still active.

- [ ] **Step 5: Run WebUI tests and syntax checks**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest tests/contract/test_workbench_api.py tests/unit/test_service_shutdown_js.py -q
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/service-shutdown.js
```

Expected: tests PASS and both Node checks exit `0`.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/voice_pipeline/webui tests/contract/test_workbench_api.py
git commit -m "feat: shut down local services from webui"
```

### Task 4: End-to-End Verification and Service Restoration

**Files:**
- Modify only if a verification failure exposes a product defect.

**Interfaces:**
- Consumes: launcher, WebUI button assets, existing lifecycle endpoint.
- Produces: verified local real-mode service restored at `http://127.0.0.1:8765/`.

- [ ] **Step 1: Run static and non-GPU regression gates**

```powershell
uv run --python .\.venv-control\Scripts\python.exe ruff check src tests
uv run --python .\.venv-control\Scripts\python.exe mypy src workers
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/service-shutdown.js
uv run --python .\.venv-control\Scripts\python.exe pytest -q -m "not gpu and not gpu_residency and not quality_model"
git diff --check
```

Expected: every command exits `0`.

- [ ] **Step 2: Exercise the BAT from another current directory**

Stop the current service through the shutdown API. From `%TEMP%`, set
`VOICE_PIPELINE_NO_BROWSER=1` and call the BAT by absolute path. Verify health becomes `ready` and the
mode is `real`.

- [ ] **Step 3: Verify full-process shutdown**

Read `runtime/run/processes.json`, invoke `POST /api/v1/control/shutdown`, wait for the monotonic
shutdown deadline, and verify the recorded control/model PIDs are gone and ports 8765, 9871, and 9880
have no listeners.

- [ ] **Step 4: Restore the user's service**

Run the BAT again with `VOICE_PIPELINE_NO_BROWSER=1`; verify `/api/v1/health` reports `ready`, `real`,
quality `ready`, and dispatcher `running`.

- [ ] **Step 5: Commit any verification-only corrections**

If no files changed, do not create an empty commit. If corrections were required, rerun all Task 4
gates before committing them with a narrowly scoped message.

