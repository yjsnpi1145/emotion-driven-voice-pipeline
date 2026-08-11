# Prevent Orphan Model Workers Design

## Context

The control plane can be restarted while the exclusive GPT-SoVITS worker is still running. The
old worker then keeps `127.0.0.1:9880`, while the new control instance attempts to launch another
`api_v2.py` process and reports only `worker exited during startup`.

The incident exposed three lifecycle gaps:

1. `stop_engine()` removes the managed-process record before it proves that the process tree exited.
2. A worker PID is published only after readiness, so cancellation or control-plane death during
   startup leaves no durable ownership record.
3. Startup does not distinguish a stale worker from an unrelated application that owns the port.

## Decision

Use layered ownership recovery rather than an unconditional port kill.

### 1. Process-exit invariant

`ManagedProcess.terminate_tree()` returns `True` only after the recorded PID/create-time is no
longer alive. `RealWorkerProcessManager.stop_engine()` retains its process and identity records
until that invariant holds. If the deadline expires, it raises `ENGINE_UNAVAILABLE` with
`poison_queue=True`; the runtime must not claim `stopped_expected`.

### 2. Cancellation-safe startup and immediate registry publication

The real process manager creates the `EngineIdentity` immediately after `Popen`, before readiness
probing. It notifies the supervisor whenever process ownership changes, causing an atomic registry
write. Startup cleanup runs for ordinary errors and `asyncio.CancelledError`; ownership is cleared
only after the child is confirmed dead.

### 3. Safe port-owner reconciliation

Before `Popen`, inspect loopback listeners on the configured engine port:

- If the listener command line exactly matches the configured Python executable and worker entry
  point for this repository, and its parent control process no longer exists, treat it as an orphan,
  terminate its process tree, and verify that the port is free.
- If the process is foreign, ambiguous, still parented, or cannot be inspected, do not terminate it.
  Return `ENGINE_UNAVAILABLE` containing engine, host, port, and owner PID.

The matcher is path-based and uses PID/create-time verification to avoid PID-reuse and substring
matches. Automatic recovery is restricted to loopback endpoints.

### 4. Launcher recovery

`scripts/start.ps1` must consider recorded worker PIDs when a stale control registry is found. It
must not archive that registry merely because the old control PID is dead. A live recorded worker
is left for the Python reconciliation path, and a second live control instance remains rejected.

## Error Handling

- Foreign port owner: deterministic `ENGINE_UNAVAILABLE`, non-retryable, with structured owner
  details; no new child is launched.
- Matching orphan cannot be stopped: deterministic `ENGINE_UNAVAILABLE`, queue-poisoning, with the
  still-live PID.
- Startup cancelled after spawn: clean process tree, publish cleared registry, then re-raise
  cancellation.
- Stop deadline exceeded: keep identity visible in health/registry and surface the failure.

## Verification

1. Unit tests prove records are retained when termination fails.
2. Unit tests prove cancellation after spawn cleans the child and ownership state.
3. Unit tests prove exact-match orphans are reaped and foreign listeners are untouched.
4. Supervisor tests prove the registry contains a starting PID before readiness completes.
5. Existing process, unit, integration, lint, and type-check suites remain green.
6. Real-service smoke test switches IndexTTS to GPT-SoVITS, stops all services, restarts, and confirms
   no workspace worker remains on ports 9871/9880.

