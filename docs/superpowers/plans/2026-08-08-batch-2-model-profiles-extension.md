# Batch 2 Model Profiles Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add imported, immutable GPT-SoVITS model profiles and deterministic official-API hot switching to the persistent Batch 2 job/version system.

**Architecture:** This extends `2026-08-07-batch-2-reliable-jobs-and-artifact-versions.md`; it must not create a second state store. SQLite owns profile metadata and active selection, the model library owns immutable bytes, and the existing single GPU queue owns the complete load-pair-and-synthesize transaction. GPT-SoVITS continues to load weights through its official HTTP endpoints.

**Tech Stack:** Python 3.11, FastAPI/Pydantic, SQLAlchemy/Alembic/aiosqlite from the parent Batch 2 plan, HTTPX, SHA-256, SQLite and Pytest.

## Global Constraints

- Base commit: `da0d35a0f4149345bda813ba523fb1bf719e64a4`; Batch 1 golden listening is `waived_by_user`, never `PASS`.
- Preserve loopback-only `127.0.0.1:8765`, public Batch 1 API envelopes, CLI-over-HTTP and one global GPU consumer.
- A profile has exactly one copied `.ckpt` and one copied `.pth`; no upload endpoint, directory scanning or source-path runtime dependency.
- Profile root is `D:\TTSsystem\models\gpt-sovits\profiles\<profile-id>\{profile.json,GPT\model.ckpt,SoVITS\model.pth}`.
- Weight bytes are immutable. Re-import creates another UUID; artifact retention never deletes profiles.
- `base` remains a selectable profile. A switch failure, file error or incompatible pair fails the job without selecting `base`.
- Reuse GPT-SoVITS `/set_gpt_weights`, `/set_sovits_weights` and `/tts`; never copy its inference implementation into the control process.
- Each GSV job, artifact version, manifest and cache key contains profile ID, relative GPT/SoVITS paths, both SHA-256 values and engine fingerprint.
- Every production change begins with a failing test, then minimal implementation, a green focused test and a commit.

## Parent-plan amendments

| Parent task | Required addition |
| --- | --- |
| Task 0 | Accept engineering `PASS` plus `golden_listening: waived_by_user`; record the waiver and never invent a golden result. |
| Task 2 | Add profile request/response/snapshot types, profile error codes and optional `model_profile_id` on GSV input. |
| Task 3 | Add `model_profiles`/`project_settings`; add immutable snapshot JSON/SHA to generation jobs and versions. |
| Tasks 4–8 | Resolve the active profile while inserting the job; retries copy the same snapshot; include it in cache/manifest/version commit. |
| Task 11 | Mount profile HTTP routes and Typer commands, configure the model root/import roots, expose only safe profile status. |
| Tasks 12–13 | Add crash, endpoint-order, no-fallback and real-local-profile gates. |

## File map

```text
Create
  src/voice_pipeline/models/model_profiles.py
  src/voice_pipeline/storage/model_profile_store.py
  src/voice_pipeline/storage/model_importer.py
  src/voice_pipeline/core/model_profile_service.py
  src/voice_pipeline/api/model_profile_routes.py
  tests/unit/test_model_profiles.py
  tests/unit/test_model_importer.py
  tests/integration_cpu/test_model_profile_api.py
  tests/integration_cpu/test_gsv_model_switch.py
  tests/process/test_model_profile_recovery.py

Modify
  src/voice_pipeline/models/{schemas.py,ports.py}
  src/voice_pipeline/core/{config.py,errors.py,pipeline.py,job_executor.py}
  src/voice_pipeline/modules/gpt_sovits/{client.py,fake.py}
  src/voice_pipeline/storage/{orm.py,job_store.py,version_store.py,cache_store.py,recovery.py}
  src/voice_pipeline/storage/migrations/versions/0001_batch2_foundation.py
  src/voice_pipeline/api/{app.py,dependencies.py,routes.py}
  src/voice_pipeline/cli.py
  config/{app.example.yaml,app.fake.yaml,open-source-reuse.yaml}
  docs/batch-2-open-source-reuse.md
```

## Frozen interfaces

```python
# models/model_profiles.py
class ModelProfileSnapshot(StrictModel):
    profile_id: UUID
    display_name: NonBlankText
    gpt_relative_path: PurePosixPath
    sovits_relative_path: PurePosixPath
    gpt_sha256: Sha256
    sovits_sha256: Sha256
    engine_fingerprint: EngineFingerprint

class ImportModelProfileRequest(StrictModel):
    display_name: NonBlankText
    gpt_source_path: Path
    sovits_source_path: Path
    declared_family: str | None = None

class ModelProfileView(ModelProfileSnapshot):
    status: Literal["ready", "missing", "corrupt", "archived"]
    created_at_utc: datetime
    active: bool

# models/schemas.py
class GsvSynthesisRequest(StrictModel):
    # preserve every existing field
    model_profile_id: UUID | None = None

# models/ports.py
class GptSoVitsClient(Protocol):
    async def load_profile(self, profile: ModelProfileSnapshot) -> None: ...
    async def synthesize(self, request: GsvSynthesisRequest, output_path: Path) -> AudioResult: ...

# storage/model_profile_store.py
class ModelProfileStore(Protocol):
    async def insert_published(self, record: ModelProfileRecord) -> ModelProfileSnapshot: ...
    async def get_ready_snapshot(self, profile_id: UUID) -> ModelProfileSnapshot: ...
    async def resolve_active_snapshot(self) -> ModelProfileSnapshot: ...
    async def activate(self, profile_id: UUID) -> ModelProfileView: ...
    async def list(self) -> list[ModelProfileView]: ...
```

A snapshot holds no caller-supplied absolute source path. The importer maps its relative paths only beneath its own `models_root`, verifies neither resolves outside it and hashes both files before returning `ready`.

## Task 0: Record Batch 1 golden-waiver prerequisite

**Files:** modify `docs/superpowers/plans/2026-08-07-batch-2-reliable-jobs-and-artifact-versions.md`; test `tests/contract/test_delivery_configuration.py`.

- [ ] **Step 1: Write the failing receipt test.**

```python
def test_batch1_handoff_allows_explicit_user_golden_waiver() -> None:
    receipt = Batch1AcceptanceReceipt.model_validate({
        "schema_version": 1, "commit_sha": "a" * 40,
        "engineering_disposition": "PASS", "golden_listening": "waived_by_user",
        "waiver_reason": "user explicitly requested to skip golden acceptance",
    })
    assert receipt.golden_listening == "waived_by_user"
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/contract/test_delivery_configuration.py::test_batch1_handoff_allows_explicit_user_golden_waiver -q`; expected failure: `Batch1AcceptanceReceipt` absent.
- [ ] **Step 3: Implement the schema and receipt.** Reject engineering states other than `PASS`; accept only `PASS|waived_by_user` golden states; require a nonblank waiver reason. Amend parent Task 0 to require engineering evidence and accept this explicit waiver.
- [ ] **Step 4: Verify GREEN.** Run `uv run pytest tests/contract/test_delivery_configuration.py -q`; expected PASS.
- [ ] **Step 5: Commit.** `git add docs/superpowers/plans/2026-08-07-batch-2-reliable-jobs-and-artifact-versions.md tests/contract/test_delivery_configuration.py && git commit -m "docs: record batch one golden waiver"`

## Task 1: Define model-profile data, configuration and errors

**Files:** create `models/model_profiles.py`, tests `tests/unit/test_model_profiles.py`; modify `core/config.py`, `core/errors.py`, `models/schemas.py`, `tests/unit/test_config.py`, `tests/unit/test_schemas.py`.

- [ ] **Step 1: Write failing validation tests.**

```python
def test_import_requires_ckpt_and_pth(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=".ckpt"):
        ImportModelProfileRequest(display_name="voice-v1", gpt_source_path=tmp_path / "g.bin", sovits_source_path=tmp_path / "s.pth")


def test_gsv_input_optionally_selects_profile() -> None:
    request = GsvSynthesisRequest.model_validate({**VALID_GSV, "model_profile_id": str(uuid4())})
    assert request.model_profile_id is not None
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/unit/test_model_profiles.py tests/unit/test_config.py tests/unit/test_schemas.py -q`; expected missing profile symbols.
- [ ] **Step 3: Implement.** Add `ModelLibrarySettings(models_root, allowed_import_roots)` and resolve them against YAML. Reject an overlap with runtime directory. Add `MODEL_PROFILE_NOT_FOUND`, `MODEL_PROFILE_UNAVAILABLE`, `MODEL_IMPORT_INVALID`, `MODEL_IMPORT_FAILED`, `MODEL_SWITCH_FAILED`. Validate nonblank display names, absolute paths and case-insensitive exact extensions; only importer checks existence.
- [ ] **Step 4: Verify GREEN.** Re-run the Step 2 command; expected PASS.
- [ ] **Step 5: Commit.** `git add src/voice_pipeline/models src/voice_pipeline/core/config.py src/voice_pipeline/core/errors.py tests/unit && git commit -m "feat: define immutable GSV model profiles"`

## Task 2: Add atomic importer and persistent profile store

**Files:** create `storage/model_importer.py`, `storage/model_profile_store.py`, tests `tests/unit/test_model_importer.py`; modify ORM, 0001 migration, recovery, persistence/migration tests.

- [ ] **Step 1: Write failing importer/repository tests.**

```python
async def test_import_copies_pair_and_source_mutation_is_irrelevant(tmp_path: Path) -> None:
    gpt = tmp_path / "in.ckpt"; gpt.write_bytes(b"gpt-v1")
    sovits = tmp_path / "in.pth"; sovits.write_bytes(b"sovits-v1")
    view = await service.import_profile(ImportModelProfileRequest(display_name="voice-v1", gpt_source_path=gpt, sovits_source_path=sovits))
    gpt.write_bytes(b"changed")
    snap = await store.get_ready_snapshot(view.profile_id)
    assert sha256_file(root / snap.gpt_relative_path) == view.gpt_sha256

async def test_failed_copy_has_no_db_row_or_visible_directory() -> None:
    with pytest.raises(PipelineError, match="MODEL_IMPORT"):
        await service.import_profile(REQUEST_WITH_MISSING_SOVITS)
    assert await store.list() == []
    assert list((root / "profiles").glob("*")) == []
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/unit/test_model_importer.py tests/unit/test_persistence_models.py -q`; expected importer/store missing.
- [ ] **Step 3: Implement.** Copy with bounded chunks, `flush()` and `os.fsync()`, then calculate SHA-256. Require normal, nonempty sources whose resolved path lies under an allowed import root. Write sorted UTF-8 JSON in `<root>/.staging/<import-id>`, fsync where supported, rename to `profiles/<uuid>`, then insert only relative paths/hashes in one SQLite transaction. On a failure delete only the owned staging tree. Migration creates `model_profiles` and singleton `project_settings`; `activate()` rehashes pair before atomically replacing the active pointer. Recovery removes unregistered staging, reports published-orphan directories, and marks damaged registered profiles instead of selecting them.
- [ ] **Step 4: Verify GREEN.** Run `uv run pytest tests/unit/test_model_importer.py tests/unit/test_persistence_models.py tests/integration_cpu/test_database_migrations.py -q`; expected PASS.
- [ ] **Step 5: Commit.** `git add src/voice_pipeline/storage tests/unit/test_model_importer.py tests/unit/test_persistence_models.py tests/integration_cpu/test_database_migrations.py && git commit -m "feat: persist and atomically import GSV model profiles"`

## Task 3: Freeze selected profile in durable jobs, versions and cache

**Files:** modify job/version/cache stores, `core/job_executor.py`, cache-key module; tests `test_persistent_jobs.py`, `test_cache_integration.py`, `test_segment_versions.py`.

- [ ] **Step 1: Write failing snapshot/cache tests.**

```python
async def test_job_uses_active_profile_at_submit_not_execution_time() -> None:
    before = await import_and_activate("voice-a")
    job = await submit_gsv_without_profile_id()
    await import_and_activate("voice-b")
    assert (await jobs.get(job.job_id)).model_profile_snapshot.profile_id == before.profile_id

def test_gsv_cache_key_changes_when_only_profile_hash_changes() -> None:
    assert gsv_cache_key(BASE, PROFILE_A) != gsv_cache_key(BASE, PROFILE_B)
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/integration_cpu/test_persistent_jobs.py tests/integration_cpu/test_cache_integration.py -q`; expected missing snapshot/cache behavior.
- [ ] **Step 3: Implement.** Resolve explicit profile or active pointer in the same transaction as GSV/segment job creation. Store canonical snapshot JSON and SHA in `generation_jobs`; retry copies it. Copy snapshot to version/run manifest. Add ID, two relative paths and two SHA values to GSV cache canonical payload before hashing. Do not touch reference cache identity or parent-reference protection.
- [ ] **Step 4: Verify GREEN.** Run `uv run pytest tests/integration_cpu/test_persistent_jobs.py tests/integration_cpu/test_cache_integration.py tests/integration_cpu/test_segment_versions.py -q`; expected PASS.
- [ ] **Step 5: Commit.** `git add src/voice_pipeline/storage src/voice_pipeline/core/job_executor.py src/voice_pipeline/modules/cache tests/integration_cpu && git commit -m "feat: freeze GSV model identity in jobs and cache"`

## Task 4: Hot-switch a pair through the official GSV endpoints

**Files:** modify GSV port/client/fake/pipeline; test `tests/contract/test_gpt_sovits_client.py`, `tests/integration_cpu/test_gsv_model_switch.py`.

- [ ] **Step 1: Write failing endpoint-order/error tests.**

```python
async def test_client_loads_official_pair_before_tts(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    events: list[str] = []
    respx_mock.get("http://gsv/set_gpt_weights").mock(side_effect=lambda request: events.append("gpt") or httpx.Response(200))
    respx_mock.get("http://gsv/set_sovits_weights").mock(side_effect=lambda request: events.append("sovits") or httpx.Response(200))
    respx_mock.post("http://gsv/tts").mock(side_effect=lambda request: events.append("tts") or wav_response())
    await client.load_profile(PROFILE)
    await client.synthesize(REQUEST, tmp_path / "target.wav")
    assert events == ["gpt", "sovits", "tts"]

async def test_second_load_failure_aborts_and_never_calls_tts() -> None:
    fake.fail_sovits_switch = True
    with pytest.raises(PipelineError, match="MODEL_SWITCH_FAILED"):
        await service.generate_gsv(CONTEXT, REQUEST)
    assert fake.tts_calls == 0
    assert runtime.abort_calls == ["gpt_sovits"]
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/contract/test_gpt_sovits_client.py tests/integration_cpu/test_gsv_model_switch.py -q`; expected `load_profile` missing.
- [ ] **Step 3: Implement.** `load_profile()` makes URL-encoded GET calls to `/set_gpt_weights` then `/set_sovits_weights` with absolute library paths. Any non-2xx, timeout, reset, malformed response or cancellation raises `PipelineError(MODEL_SWITCH_FAILED, requires_engine_abort=True)`. At the GSV inference boundary validate snapshot hashes, begin the existing runtime inference lease, always load both weights, then synthesize. Any unknown outcome triggers the existing abort/poison protocol; do not use an “already loaded” optimization.
- [ ] **Step 4: Verify GREEN.** Run `uv run pytest tests/contract/test_gpt_sovits_client.py tests/integration_cpu/test_gsv_model_switch.py tests/integration_cpu/test_multi_cli_gpu_mutex.py tests/unit/test_pipeline.py -q`; expected PASS.
- [ ] **Step 5: Commit.** `git add src/voice_pipeline/models/ports.py src/voice_pipeline/modules/gpt_sovits src/voice_pipeline/core/pipeline.py tests/contract/test_gpt_sovits_client.py tests/integration_cpu/test_gsv_model_switch.py tests/unit/test_pipeline.py && git commit -m "feat: switch GPT-SoVITS profiles before synthesis"`

## Task 5: Provide HTTP and CLI control without a UI

**Files:** create model profile routes and API test; modify app/dependencies/routes/CLI/config/CLI contract.

- [ ] **Step 1: Write failing HTTP/CLI tests.**

```python
async def test_import_activate_and_submit_snapshots_profile(client: AsyncClient, sources: dict[str, str]) -> None:
    created = await client.post("/api/v1/model-profiles/import", json={"display_name": "voice-v1", **sources})
    assert created.status_code == 201
    profile_id = created.json()["profile_id"]
    assert (await client.post(f"/api/v1/model-profiles/{profile_id}/activate")).status_code == 200
    job = await client.post("/api/v1/jobs/gsv", json=GSV_WITHOUT_PROFILE)
    assert (await client.get(job.json()["status_url"])).json()["model_profile_snapshot"]["profile_id"] == profile_id

def test_cli_import_posts_to_http_server(runner: CliRunner) -> None:
    assert runner.invoke(app, ["model", "list"]).exit_code == 0
    assert fake_http.last_request.path == "/api/v1/model-profiles"
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/integration_cpu/test_model_profile_api.py tests/contract/test_cli_json_contract.py -q`; expected 404/unknown command.
- [ ] **Step 3: Implement.** Mount only `POST /model-profiles/import` (201), `GET /model-profiles`, `GET /model-profiles/{id}`, `POST /model-profiles/{id}/activate`. Use structured 422 input errors and 409 unavailable-profile activation. The Typer `model import|list|activate` commands make HTTP requests only. Add the snapshot only as an additive job-status field. Configurations use explicit library/import roots; test roots are temporary fixtures.
- [ ] **Step 4: Verify GREEN.** Run `uv run pytest tests/integration_cpu/test_model_profile_api.py tests/contract/test_cli_json_contract.py tests/integration_cpu/test_api_jobs.py tests/integration_cpu/test_api_failures.py -q`; expected PASS.
- [ ] **Step 5: Commit.** `git add src/voice_pipeline/api src/voice_pipeline/cli.py config tests/integration_cpu/test_model_profile_api.py tests/contract/test_cli_json_contract.py && git commit -m "feat: expose local model profile controls"`

## Task 6: Test recovery, independent black box and user-selected real pair

**Files:** create process/acceptance tests; modify verification/runbook/README.

- [ ] **Step 1: Write failing process and black-box tests.**

```python
def test_kill_after_directory_publish_never_exposes_half_profile(harness: ControlHarness) -> None:
    harness.kill_importer_at("profile_directory_published")
    harness.restart()
    assert harness.http.get("/api/v1/model-profiles").json() == []
    assert harness.visible_profile_directories() == []

def test_switch_failure_never_falls_back_to_base(external_server: FakeGsvServer) -> None:
    profile = external_server.import_profile("custom")
    external_server.fail_next("/set_sovits_weights")
    assert external_server.wait(external_server.submit(profile))["status"] == "failed"
    assert external_server.events == ["set_gpt_weights", "set_sovits_weights"]
    assert external_server.base_tts_calls == 0
```

- [ ] **Step 2: Verify RED.** Run `uv run pytest tests/process/test_model_profile_recovery.py .acceptance/batch2_model_profiles -q`; expected missing crash/black-box harness behavior.
- [ ] **Step 3: Implement and document.** Emit production audit/checkpoint events after staging fsync, profile-directory publish and DB commit; no test-only HTTP route. Extend the external fake server with process-shared exact endpoint logs. Document import, activation, corruption diagnosis and backup; state that profile directories are independent of artifact retention.
- [ ] **Step 4: Verify all CPU gates.**

```powershell
uv lock --check
uv run ruff check src tests workers
uv run mypy src/voice_pipeline workers
uv run pytest tests/unit tests/contract tests/integration_cpu tests/process -m 'not gpu and not gpu_residency' -q -W error
uv run pytest .acceptance/batch2_model_profiles -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Verify one actual local training pair.**

```powershell
voice-pipeline model import --name "<selected-name>" --gpt "<absolute .ckpt>" --sovits "<absolute .pth>"
voice-pipeline model activate <returned-profile-id>
# Submit one existing reference/GSV job through HTTP and inspect its run manifest.
```

Expected: imported `profile.json`, job snapshot and run manifest have identical profile ID and SHA-256. This is an engineering smoke, not a replacement for the waived human golden listening gate.

- [ ] **Step 6: Commit and hand off.** `git add scripts docs README.md tests/process .acceptance/batch2_model_profiles && git commit -m "test: verify model profile recovery and switching"`; then write `runtime/handoff/batch2-developer-report.json` with evidence and `real_training_profile: PASS|BLOCKED`.

## Self-review

The six tasks cover the new design’s data, files, official switching, API/CLI, snapshots/cache and recovery. They amend every affected parent task, preserve previous public routes, and explicitly leave UI, auto-pairing and model-impact workflows to batches 4–5.
