# Prevent Orphan Model Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale IndexTTS/GPT-SoVITS workers from surviving control-plane restarts or causing opaque startup failures.

**Architecture:** Strengthen the managed-process exit invariant, publish worker ownership as soon as a process is spawned, and reconcile only exact-match orphan listeners before launch. Ambiguous or foreign listeners are reported but never killed.

**Tech Stack:** Python 3.11, asyncio, psutil, pytest, PowerShell launcher

## Global Constraints

- All engine and control endpoints remain loopback-only.
- A PID is acted on only when its create-time still matches the observed process.
- Automatic orphan cleanup requires an exact configured executable and entry-point match.
- A worker is never reported stopped until its recorded process tree is confirmed dead.
- Tests are written and observed failing before production changes.

---

### Task 1: Freeze managed-process lifecycle failures

**Files:**
- Create: `tests/unit/test_worker_process_manager.py`
- Modify: `src/voice_pipeline/runtime/process.py`

**Interfaces:**
- Produces: `ManagedProcess.terminate_tree(*, timeout: float) -> bool`
- Produces: `RealWorkerProcessManager.set_state_change_callback(callback: Callable[[], None]) -> None`

- [ ] Write tests that assert failed termination retains identity, startup cancellation cleans the
  child, and ownership callbacks occur at spawn/clear boundaries.
- [ ] Run the focused tests and confirm they fail on the current implementation.
- [ ] Make termination deadline-aware and keep records until confirmed exit.
- [ ] Create identity immediately after spawn and clean up under `BaseException` before re-raising.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Add safe loopback listener reconciliation

**Files:**
- Create: `src/voice_pipeline/runtime/port_recovery.py`
- Create: `tests/unit/test_port_recovery.py`
- Modify: `src/voice_pipeline/runtime/process.py`

**Interfaces:**
- Produces: `LoopbackPortReconciler.ensure_available(spec: WorkerLaunchSpec) -> None`
- Consumes: configured Python path, entry-point path/module, host, port, and current control PID.

- [ ] Write tests for an exact-match parentless worker, a foreign listener, PID reuse, and an
  inaccessible owner.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement path-normalized exact matching and verified tree termination.
- [ ] Call reconciliation immediately before each worker `Popen`.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Publish starting PIDs through the supervisor

**Files:**
- Modify: `src/voice_pipeline/runtime/supervisor.py`
- Modify: `tests/process/test_supervisor.py`

**Interfaces:**
- Consumes: optional `set_state_change_callback()` on a process manager.
- Produces: atomic `processes.json` updates when a worker is spawned or cleared.

- [ ] Add a gated-start test proving the registry exposes the PID before readiness completes.
- [ ] Run the test and confirm it fails.
- [ ] Bind the process-manager ownership callback to `_write_registry()`.
- [ ] Run supervisor tests and confirm they pass.

### Task 4: Harden stale launcher handling

**Files:**
- Modify: `scripts/start.ps1`
- Create: `tests/unit/test_start_script_worker_registry.py`

**Interfaces:**
- Consumes: `runtime/run/processes.json` worker PID/create-time entries.
- Produces: stale-run classification that does not discard live worker ownership.

- [ ] Add source-level and process-backed tests for a dead control with a live recorded worker.
- [ ] Run tests and confirm the current launcher archives the registry incorrectly.
- [ ] Include worker identities in stale-registry validation without killing ambiguous processes.
- [ ] Run the launcher-focused tests and confirm they pass.

### Task 5: Regression and real-runtime verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-prevent-orphan-workers-design.md` only if verified
  behavior differs from the design.

**Interfaces:**
- Consumes: all lifecycle changes above.
- Produces: evidence that the original port-9880 failure cannot recur through supported shutdown
  and can recover from an exact-match orphan.

- [ ] Run focused unit/process tests.
- [ ] Run `ruff check`, `mypy`, and the full CPU-safe test suite.
- [ ] Stop the local service, verify ports 8765/9871/9880 and recorded PIDs are clear, then restart.
- [ ] Exercise an exclusive IndexTTS-to-GPT-SoVITS switch and verify the old worker exits.
- [ ] Commit, push, create a PR, wait for CI, merge, and restart the service from `main`.

