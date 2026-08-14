# Multi-Role Director Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a durable two-stage director workflow that analyzes long scripts, lets the user review and reassign speakers, maps reusable role presets, then performs fault-isolated multi-role synthesis through the existing IndexTTS2 and GPT-SoVITS pipeline.

**Architecture:** Add a director domain beside the existing single-role chapter domain. SQLite stores projects, analysis chunks, roles, utterances, audit events, role presets, and immutable generation runs. The LLM works in resumable analysis and translation phases; confirmed utterances are materialized into existing low-level tasks/segments, then references are generated before model-grouped GSV jobs and final composition.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy asyncio, Alembic, SQLite, vanilla JavaScript/CSS, pytest, httpx, existing OpenAI-compatible director, IndexTTS2 and GPT-SoVITS adapters.

## Global Constraints

- Preserve the existing single-role chapter API and WebUI behavior.
- Bind HTTP only to `127.0.0.1`; CLI and WebUI remain HTTP clients of the control plane.
- Keep exactly one GPU consumer and retain existing timeout/abort semantics.
- LLM analysis never starts GPU inference.
- Every source character is covered exactly once and source order never changes.
- Low-confidence assignments require explicit user confirmation.
- Narration is detected by default and can be globally excluded without deleting text.
- Role presets contain only preset name, managed base audio, GPT-SoVITS profile, and default speed.
- The IndexTTS2 base audio has no 3--10 second restriction; generated GSV references retain the existing window checks.
- Emotion vectors remain exact eight-value inputs with the existing per-value and total constraints.
- Every mutation uses optimistic concurrency; late LLM results never overwrite user edits.
- One utterance or one model failure cannot stop other executable utterances.
- Existing succeeded versions and cache entries are reused after restart.
- First release excludes overlapping voices, SFX/music mixing, lip sync, automatic voice selection from prose descriptions, multi-user operation, and public deployment.

---

## File Structure

New focused modules:

- `src/voice_pipeline/models/director.py` — director API/domain Pydantic contracts.
- `src/voice_pipeline/models/director_llm.py` — staged LLM request/response contracts.
- `src/voice_pipeline/storage/director_store.py` — project, role, utterance, revision and audit transactions.
- `src/voice_pipeline/storage/role_preset_store.py` — role preset metadata and integrity state.
- `src/voice_pipeline/core/director_analysis.py` — chunk analysis, cast reconciliation and translation orchestration.
- `src/voice_pipeline/core/role_preset_service.py` — managed audio import and model profile resolution.
- `src/voice_pipeline/core/director_generation.py` — immutable run creation, two-phase dispatch, failure isolation and composition.
- `src/voice_pipeline/modules/llm/script_chunking.py` — deterministic source-preserving chunker and validators.
- `src/voice_pipeline/api/director_routes.py` — public director and role preset endpoints.
- `src/voice_pipeline/webui/director.js` — director UI state, requests and rendering.
- `src/voice_pipeline/webui/director-dnd.js` — pure drag/drop and bulk-assignment helpers.
- `src/voice_pipeline/storage/migrations/versions/0004_director_mode.py` — director schema migration.

Existing integration points:

- `src/voice_pipeline/storage/orm.py`
- `src/voice_pipeline/storage/database.py`
- `src/voice_pipeline/core/config.py`
- `src/voice_pipeline/core/errors.py`
- `src/voice_pipeline/modules/llm/models.py`
- `src/voice_pipeline/modules/llm/client.py`
- `src/voice_pipeline/modules/llm/fake.py`
- `src/voice_pipeline/modules/llm/runtime.py`
- `src/voice_pipeline/modules/llm/activity.py`
- `src/voice_pipeline/api/app.py`
- `src/voice_pipeline/webui/index.html`
- `src/voice_pipeline/webui/app.js`
- `src/voice_pipeline/webui/styles.css`
- `config/app.example.yaml`

---

### Task 1: Director Domain Contracts and Database Migration

**Files:**
- Create: `src/voice_pipeline/models/director.py`
- Create: `src/voice_pipeline/models/director_llm.py`
- Create: `src/voice_pipeline/storage/migrations/versions/0004_director_mode.py`
- Modify: `src/voice_pipeline/storage/orm.py`
- Modify: `src/voice_pipeline/storage/database.py`
- Modify: `src/voice_pipeline/core/errors.py`
- Test: `tests/unit/test_director_models.py`
- Test: `tests/integration_cpu/test_director_migration.py`

**Interfaces:**
- Produces `DirectorProjectRecord`, `DirectorRoleRecord`, `DirectorUtteranceRecord`, `RolePresetRecord`, `DirectorGenerationRecord` and strict request types.
- Produces ORM tables named `director_projects`, `director_analysis_chunks`, `director_roles`, `director_utterances`, `director_edit_events`, `role_presets`, `director_generations`, and `director_generation_items`.
- Advances `PACKAGED_HEAD` to `0004_director_mode`.

- [ ] **Step 1: Write failing model tests**

```python
def test_director_utterance_requires_exact_forward_range() -> None:
    with pytest.raises(ValidationError):
        DirectorUtteranceRecord.model_validate({
            **valid_utterance(), "source_start": 5, "source_end": 5
        })


def test_role_preset_rejects_non_wav_managed_path() -> None:
    with pytest.raises(ValidationError):
        CreateRolePresetRequest(
            name="林雪", base_voice_path=Path("voice.mp3"),
            model_profile_id=uuid4(), default_speed=1.0,
        )
```

- [ ] **Step 2: Run model tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_models.py -q`  
Expected: collection failure because `voice_pipeline.models.director` does not exist.

- [ ] **Step 3: Implement strict domain contracts**

Define exact literals and requests:

```python
DirectorProjectStatus = Literal[
    "draft", "analyzing", "role_review", "translating",
    "translation_review", "voice_mapping", "ready", "generating",
    "generation_incomplete", "succeeded",
]
UtteranceKind = Literal["dialogue", "narration", "stage_direction"]

class DirectorUtterancePatch(StrictModel):
    expected_revision: int = Field(ge=0)
    role_id: UUID | None = None
    speak_enabled: bool | None = None
    role_confirmed: bool | None = None
    synthesis_text: NonBlankText | None = None
    ref_text_cn: ChineseReferenceText | None = None
    emotion_vector: EmotionVector | None = None
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    pause_after_ms: int | None = Field(default=None, ge=0, le=30_000)
```

Use one shared `SourceLanguage = Literal["auto", "zh", "ja", "en", "ko", "yue"]`; generation target remains the existing `LanguageCode`.

- [ ] **Step 4: Write failing migration test**

```python
@pytest.mark.asyncio
async def test_director_migration_creates_all_tables(tmp_path, storage_settings):
    db = await Database.open(storage_settings, instance_id=uuid4(), migrate=True)
    try:
        assert await db.alembic_revision() == "0004_director_mode"
        names = await table_names(db)
        assert {
            "director_projects", "director_analysis_chunks", "director_roles",
            "director_utterances", "director_edit_events", "role_presets",
            "director_generations", "director_generation_items",
        } <= names
    finally:
        await db.close()
```

- [ ] **Step 5: Run migration test and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/integration_cpu/test_director_migration.py -q`  
Expected: revision remains `0003_chapter_history_soft_delete`.

- [ ] **Step 6: Add ORM tables and migration**

Use string UUID foreign keys and JSON text columns consistent with existing tables. Required constraints include:

```python
UniqueConstraint("project_id", "ordinal", name="uq_director_utterances_project_ordinal")
CheckConstraint("source_start >= 0", name="ck_director_utterances_start")
CheckConstraint("source_end > source_start", name="ck_director_utterances_range")
CheckConstraint("revision >= 0", name="ck_director_utterances_revision")
UniqueConstraint("project_id", "project_revision", name="uq_director_generation_revision")
```

Migration `down_revision` is `0003_chapter_history_soft_delete`; `upgrade()` creates tables in dependency order and `downgrade()` drops them in reverse order.

- [ ] **Step 7: Add director-specific error codes**

```python
DIRECTOR_STATE_CONFLICT = "DIRECTOR_STATE_CONFLICT"
DIRECTOR_REVIEW_REQUIRED = "DIRECTOR_REVIEW_REQUIRED"
DIRECTOR_SOURCE_COVERAGE_INVALID = "DIRECTOR_SOURCE_COVERAGE_INVALID"
ROLE_PRESET_UNAVAILABLE = "ROLE_PRESET_UNAVAILABLE"
```

- [ ] **Step 8: Run Task 1 tests and static checks**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py -q`  
Expected: PASS.  
Run: `.venv/Scripts/python.exe -m ruff check src/voice_pipeline/models/director.py src/voice_pipeline/models/director_llm.py src/voice_pipeline/storage/orm.py src/voice_pipeline/storage/migrations/versions/0004_director_mode.py tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py`  
Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/voice_pipeline/models/director.py src/voice_pipeline/models/director_llm.py src/voice_pipeline/storage/orm.py src/voice_pipeline/storage/database.py src/voice_pipeline/storage/migrations/versions/0004_director_mode.py src/voice_pipeline/core/errors.py tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py
git commit -m "feat: add director domain schema"
```

### Task 2: Director Store, Source Invariants, Revisions and Audit Events

**Files:**
- Create: `src/voice_pipeline/storage/director_store.py`
- Test: `tests/integration_cpu/test_director_store.py`
- Test: `tests/unit/test_director_source_edits.py`

**Interfaces:**
- Produces `DirectorStore.create_project`, `get_project`, `list_projects`, `begin_analysis`, `publish_analysis`, `list_roles`, `list_utterances`, `patch_utterances`, `split_utterance`, `merge_utterances`, `merge_roles`, `split_role`, `confirm_role_review`, `publish_translation`, `confirm_translation`, `bind_role_preset`, `prepare_generation`, and `append_generation_progress`.
- Every mutator consumes `expected_revision` and raises `VERSION_CONFLICT` on stale writes.

- [ ] **Step 1: Write failing project and source-coverage tests**

```python
@pytest.mark.asyncio
async def test_publish_analysis_requires_exact_contiguous_source_coverage(store):
    project = await store.create_project(create_request("甲说：你好。"))
    with pytest.raises(PipelineError) as exc:
        await store.publish_analysis(
            project.project_id, expected_revision=project.revision,
            roles=(narrator_role(),),
            utterances=(utterance(0, 0, 2, "甲说", "narration"),),
        )
    assert exc.value.code == ErrorCode.DIRECTOR_SOURCE_COVERAGE_INVALID
```

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/integration_cpu/test_director_store.py -q`  
Expected: import failure for `DirectorStore`.

- [ ] **Step 3: Implement project creation, reads and atomic analysis publication**

`publish_analysis()` must validate before opening the write transaction:

```python
def validate_source_coverage(source: str, rows: Sequence[CreateDirectorUtterance]) -> None:
    cursor = 0
    for ordinal, row in enumerate(rows):
        if row.ordinal != ordinal or row.source_start != cursor:
            raise _coverage_error()
        if source[row.source_start:row.source_end] != row.source_text:
            raise _coverage_error()
        cursor = row.source_end
    if cursor != len(source):
        raise _coverage_error()
```

Insert roles, utterances, one `analysis_published` edit event, increment project revision, and change status to `role_review` in one transaction.

- [ ] **Step 4: Write failing edit/OCC tests**

Cover:

```python
async def test_split_then_merge_preserves_source_and_ordinals(store): ...
async def test_merge_rejects_non_adjacent_utterances(store): ...
async def test_stale_drag_assignment_cannot_overwrite_newer_edit(store): ...
async def test_narration_toggle_preserves_source_rows(store): ...
async def test_role_merge_reassigns_utterances_and_appends_audit_event(store): ...
async def test_late_analysis_revision_cannot_replace_user_review(store): ...
```

- [ ] **Step 5: Run edit tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_source_edits.py tests/integration_cpu/test_director_store.py -q`  
Expected: missing edit methods.

- [ ] **Step 6: Implement split, merge, bulk assignment and role edits**

Rules implemented in transactions:

```python
if split_at <= row.source_start or split_at >= row.source_end:
    raise PipelineError(ErrorCode.INVALID_INPUT, "director", "split point is outside utterance", retryable=False)
if right.source_start != left.source_end:
    raise PipelineError(ErrorCode.INVALID_INPUT, "director", "only adjacent utterances can merge", retryable=False)
```

Renumber all later ordinals after split/merge. Bulk assignment updates only rows matching their expected revisions. Role split request contains selected utterance IDs and a new canonical name; role merge request contains source role IDs and one target role ID.

- [ ] **Step 7: Implement confirmation gates and translation publication**

`confirm_role_review()` rejects any `speak_enabled=true` utterance with `role_confirmed=false` or a missing/non-character role. `publish_translation()` compares each result's utterance revision and stores all results atomically; stale results are discarded with `VERSION_CONFLICT`. `confirm_translation()` requires every spoken row to contain valid synthesis/ref text and parameters.

- [ ] **Step 8: Run Task 2 tests and static checks**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_source_edits.py tests/integration_cpu/test_director_store.py -q`  
Expected: PASS.  
Run: `.venv/Scripts/python.exe -m ruff check src/voice_pipeline/storage/director_store.py tests/unit/test_director_source_edits.py tests/integration_cpu/test_director_store.py`  
Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add src/voice_pipeline/storage/director_store.py tests/unit/test_director_source_edits.py tests/integration_cpu/test_director_store.py
git commit -m "feat: persist director reviews and edits"
```

### Task 3: Staged LLM Contracts, Chunking and Runtime Director Support

**Files:**
- Create: `src/voice_pipeline/modules/llm/script_chunking.py`
- Modify: `src/voice_pipeline/models/director_llm.py`
- Modify: `src/voice_pipeline/modules/llm/activity.py`
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/modules/llm/fake.py`
- Modify: `src/voice_pipeline/modules/llm/runtime.py`
- Modify: `src/voice_pipeline/core/config.py`
- Modify: `src/voice_pipeline/models/runtime_settings.py`
- Modify: `config/app.example.yaml`
- Test: `tests/unit/test_script_chunking.py`
- Test: `tests/unit/test_director_llm_contracts.py`
- Test: `tests/unit/test_openai_director.py`

**Interfaces:**
- Produces `split_script(source_text, max_chars) -> tuple[ScriptChunk, ...]` and `validate_chunk_analysis`.
- Extends director protocol with `analyze_script_chunk`, `reconcile_cast`, and `translate_utterances`.
- Adds `llm.max_parallel_requests` with range 1..4 and default 2 to config and runtime settings.

- [ ] **Step 1: Write failing deterministic chunk tests**

```python
def test_script_chunks_are_contiguous_and_reconstruct_source():
    source = "第一幕\n\n林雪：你好。\n" * 100
    chunks = split_script(source, max_chars=240)
    assert "".join(item.text for item in chunks) == source
    assert chunks[0].source_start == 0
    assert chunks[-1].source_end == len(source)
    assert all(a.source_end == b.source_start for a, b in pairwise(chunks))
```

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_script_chunking.py -q`  
Expected: module does not exist.

- [ ] **Step 3: Implement source-preserving chunking and validation**

Prefer the last paragraph/newline/sentence boundary within the budget, but fall back to the hard character boundary. Never strip chunk text. `validate_chunk_analysis()` requires local ranges to start at 0, remain contiguous and end at `len(chunk.text)`.

- [ ] **Step 4: Write failing staged LLM tests**

Test exact JSON request/response behavior for:

```python
analysis = await client.analyze_script_chunk(chunk=chunk, known_roles=())
cast = await client.reconcile_cast(candidates=analysis.role_candidates)
translations = await client.translate_utterances(
    target_language="ja", utterances=(translation_input(...),)
)
```

Assert invalid ranges, unknown utterance IDs, invalid emotion totals and omitted items raise `LLM_INVALID_RESPONSE`.

- [ ] **Step 5: Run staged tests and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_llm_contracts.py tests/unit/test_openai_director.py -q`  
Expected: methods absent.

- [ ] **Step 6: Implement schemas and OpenAI-compatible prompts**

Add strict models:

```python
class ScriptChunkAnalysis(StrictModel):
    chunk_sha256: Sha256
    utterances: tuple[AnalyzedUtterance, ...]

class CastReconciliation(StrictModel):
    roles: tuple[ReconciledRole, ...]
    assignments: tuple[CandidateRoleAssignment, ...]

class UtteranceTranslationBatch(StrictModel):
    items: tuple[TranslatedUtterance, ...]
```

Use existing `_post_json()` and JSON-object response format. Add activity operations `script_analysis`, `cast_reconciliation`, and `script_translation`.

- [ ] **Step 7: Implement FakeDirector and RuntimeDirector delegation**

Fake analysis deterministically separates bracketed stage directions, `角色：台词` lines and narration while retaining all characters. Fake translation preserves text for same-language requests and returns deterministic Chinese reference text and a valid calm vector. RuntimeDirector holds its existing lock and delegates to whichever director is active.

- [ ] **Step 8: Add configurable bounded LLM concurrency**

Extend `LlmSettings`, `LlmSettingsSnapshot`, update/view models, UI payload compatibility, and config example with:

```python
max_parallel_requests: int = Field(default=2, ge=1, le=4)
```

Existing saved runtime settings missing this field use default 2.

- [ ] **Step 9: Run Task 3 tests and compatibility tests**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_script_chunking.py tests/unit/test_director_llm_contracts.py tests/unit/test_openai_director.py tests/unit/test_runtime_llm_settings.py tests/contract/test_product_settings_api.py -q`  
Expected: PASS.

- [ ] **Step 10: Commit Task 3**

```bash
git add src/voice_pipeline/modules/llm/script_chunking.py src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/activity.py src/voice_pipeline/modules/llm/client.py src/voice_pipeline/modules/llm/fake.py src/voice_pipeline/modules/llm/runtime.py src/voice_pipeline/core/config.py src/voice_pipeline/models/runtime_settings.py config/app.example.yaml tests/unit/test_script_chunking.py tests/unit/test_director_llm_contracts.py tests/unit/test_openai_director.py tests/unit/test_runtime_llm_settings.py tests/contract/test_product_settings_api.py
git commit -m "feat: analyze scripts with staged llm calls"
```

### Task 4: Director Analysis and Translation Orchestration

**Files:**
- Create: `src/voice_pipeline/core/director_analysis.py`
- Test: `tests/unit/test_director_analysis.py`
- Test: `tests/integration_cpu/test_director_analysis_recovery.py`

**Interfaces:**
- Produces `DirectorAnalysisService.analyze(project_id)`, `translate(project_id)`, `resume_pending()`, and `stop(deadline)`.
- Consumes `DirectorStore`, staged `RuntimeDirector`, and `max_parallel_requests`.

- [ ] **Step 1: Write failing analysis orchestration tests**

```python
@pytest.mark.asyncio
async def test_analysis_persists_successful_chunks_and_retries_only_failed_chunk(...):
    await service.analyze(project.project_id)
    assert client.calls_by_chunk == {0: 1, 1: 1, 2: 1}
    await service.resume_pending()
    assert client.calls_by_chunk == {0: 1, 1: 2, 2: 1}
```

Also assert GPU clients have zero calls throughout analysis and translation.

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_analysis.py -q`  
Expected: service absent.

- [ ] **Step 3: Implement bounded chunk analysis and cast reconciliation**

Create one background task per project. Use `asyncio.Semaphore(max_parallel_requests)`. Persist each validated chunk immediately. After all chunks succeed, call cast reconciliation, materialize roles/utterances with absolute offsets, and atomically publish `role_review`.

- [ ] **Step 4: Write failing translation revision tests**

```python
async def test_translation_ignores_late_result_after_user_split(...): ...
async def test_translation_reuses_completed_batches_after_restart(...): ...
async def test_translation_only_sends_spoken_utterances(...): ...
```

- [ ] **Step 5: Implement translation orchestration**

Snapshot utterance IDs and revisions before request. Publish a batch only if every row still matches. On conflict keep the project in `role_review` or `translation_review` with a structured actionable error; never overwrite user edits.

- [ ] **Step 6: Implement recovery and shutdown**

At startup, convert abandoned `analyzing`/`translating` work into resumable work and schedule only incomplete chunks/batches. `stop(deadline)` cancels active tasks without deleting persisted progress.

- [ ] **Step 7: Run Task 4 tests**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_analysis.py tests/integration_cpu/test_director_analysis_recovery.py -q`  
Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/voice_pipeline/core/director_analysis.py tests/unit/test_director_analysis.py tests/integration_cpu/test_director_analysis_recovery.py
git commit -m "feat: orchestrate durable director analysis"
```

### Task 5: Managed Role Presets

**Files:**
- Create: `src/voice_pipeline/storage/role_preset_store.py`
- Create: `src/voice_pipeline/core/role_preset_service.py`
- Modify: `src/voice_pipeline/core/config.py`
- Modify: `src/voice_pipeline/models/desktop.py`
- Modify: `src/voice_pipeline/core/desktop_service.py`
- Modify: `config/app.example.yaml`
- Test: `tests/integration_cpu/test_role_presets.py`
- Test: `tests/unit/test_role_preset_import.py`

**Interfaces:**
- Produces `RolePresetService.import_preset`, `list`, `get`, `update`, `archive`, `resolve`, and `audio_path`.
- Adds `model_library.role_presets_root`, resolved beside but not inside runtime storage.
- Adds desktop picker kind `role_base_voice` using WAV filter.

- [ ] **Step 1: Write failing import and integrity tests**

```python
async def test_import_copies_base_voice_into_managed_library(service, source_wav):
    preset = await service.import_preset(request(source_wav))
    source_wav.unlink()
    resolved = await service.resolve(preset.preset_id)
    assert resolved.base_voice_path.is_file()
    assert resolved.base_voice_sha256 == sha256_file(resolved.base_voice_path)
```

Cover duplicate content, path traversal, symlink rejection, missing model profile, archive, SHA mismatch and a one-minute input WAV.

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_role_preset_import.py tests/integration_cpu/test_role_presets.py -q`  
Expected: modules absent.

- [ ] **Step 3: Implement atomic managed import**

Import to `role_presets_root/{preset_id}/base.wav.partial`, flush/fsync, verify WAV without reference-window enforcement, hash, then `os.replace()` to `base.wav`. Insert metadata only after publication; startup reconciliation removes abandoned partials and marks missing/corrupt presets unavailable.

- [ ] **Step 4: Implement preset CRUD and model resolution**

`resolve()` returns a frozen structure containing preset ID/name, managed audio path/SHA, model profile snapshot and default speed. Updating a preset increments revision; existing generation snapshots remain unchanged.

- [ ] **Step 5: Add config and desktop picker support**

```yaml
model_library:
  models_root: ../models/gpt-sovits
  role_presets_root: ../models/characters
```

Validate neither managed model root overlaps runtime. Add `role_base_voice` to `FilePickKind` and `_PICKERS`.

- [ ] **Step 6: Run Task 5 tests**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_role_preset_import.py tests/integration_cpu/test_role_presets.py tests/unit/test_desktop_service.py tests/unit/test_config.py -q`  
Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```bash
git add src/voice_pipeline/storage/role_preset_store.py src/voice_pipeline/core/role_preset_service.py src/voice_pipeline/core/config.py src/voice_pipeline/models/desktop.py src/voice_pipeline/core/desktop_service.py config/app.example.yaml tests/unit/test_role_preset_import.py tests/integration_cpu/test_role_presets.py tests/unit/test_desktop_service.py tests/unit/test_config.py
git commit -m "feat: manage reusable role voice presets"
```

### Task 6: Director REST API and Lifecycle Wiring

**Files:**
- Create: `src/voice_pipeline/api/director_routes.py`
- Modify: `src/voice_pipeline/api/app.py`
- Modify: `src/voice_pipeline/api/routes.py`
- Test: `tests/contract/test_director_api.py`
- Test: `tests/integration_cpu/test_director_lifecycle.py`

**Interfaces:**
- Exposes every `/api/v1/director-*` and role preset endpoint frozen in the design.
- `ControlPlane` owns `director_store`, `director_analysis`, `role_presets`, and later `director_generation`.

- [ ] **Step 1: Write failing public API contract tests**

Create a project, start fake analysis, poll to `role_review`, list roles/utterances, bulk reassign with revisions, split/merge, confirm roles, translate, edit translation, confirm translation, create/bind a role preset, and verify `ready` blockers. Assert all returned payloads are path-free except the constrained local file-picker response.

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/contract/test_director_api.py -q`  
Expected: 404 for director endpoints.

- [ ] **Step 3: Implement router and error mapping**

Use existing `PipelineError` handler. Background commands return HTTP 202 with project/status/event URLs. Mutations return updated public records. SSE events use monotonically increasing event IDs and heartbeat behavior matching existing chapter events.

- [ ] **Step 4: Wire stores/services into app lifespan**

Construct services after database, LLM and model profile services. Run preset reconciliation and analysis recovery before accepting requests. Stop director analysis/generation before queue shutdown. Include `build_director_router(plane)` without altering old routes.

- [ ] **Step 5: Extend health diagnostics**

Add path-free counts:

```json
"director": {
  "active_analysis": 0,
  "active_generation": 0,
  "projects_needing_review": 0,
  "unavailable_role_presets": 0
}
```

- [ ] **Step 6: Run Task 6 tests**

Run: `.venv/Scripts/python.exe -m pytest tests/contract/test_director_api.py tests/integration_cpu/test_director_lifecycle.py tests/integration_cpu/test_app_storage_lifecycle.py -q`  
Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add src/voice_pipeline/api/director_routes.py src/voice_pipeline/api/app.py src/voice_pipeline/api/routes.py tests/contract/test_director_api.py tests/integration_cpu/test_director_lifecycle.py tests/integration_cpu/test_app_storage_lifecycle.py
git commit -m "feat: expose director review workflow"
```

### Task 7: Multi-Role Two-Phase Generation and Fault Isolation

**Files:**
- Create: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/api/director_routes.py`
- Modify: `src/voice_pipeline/api/app.py`
- Test: `tests/unit/test_director_generation.py`
- Test: `tests/integration_cpu/test_director_generation.py`
- Test: `tests/integration_cpu/test_director_generation_recovery.py`

**Interfaces:**
- Produces `DirectorGenerationService.start`, `resume`, `recompose`, `progress`, `recover`, and `stop`.
- Materializes spoken utterances into existing `DubbingTaskRecord` and `SegmentRecord` rows.
- Submits all reference jobs first; submits successful-reference GSV jobs grouped by frozen model profile.

- [ ] **Step 1: Write failing immutable snapshot and scheduling tests**

```python
async def test_generation_runs_all_references_before_grouped_gsv(...):
    run = await service.start(project_id, expected_revision=ready.revision)
    await wait_terminal(run.generation_id)
    assert events.engine_kinds == ["reference", "reference", "reference", "gsv", "gsv", "gsv"]
    assert events.gsv_profile_ids == [profile_a, profile_a, profile_b]
```

Also mutate a role preset after `start()` and assert submitted requests still use the frozen original hashes.

- [ ] **Step 2: Run and verify RED**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_generation.py -q`  
Expected: service absent.

- [ ] **Step 3: Implement readiness validation and idempotent run creation**

Reject start unless project status is `ready`, every spoken utterance has confirmed current translation, and every used role resolves to a healthy preset. In one transaction create `director_generations` plus items and move project to `generating`. A unique `(project_id, project_revision)` returns the existing generation on duplicate start.

- [ ] **Step 4: Implement materialization and reference phase**

Create one low-level task using the complete project source. Create one segment per spoken utterance with exact source ranges and frozen translated inputs. Submit reference jobs using each role's managed base voice. Await each job terminally without throwing out of the phase; store per-item error and continue.

- [ ] **Step 5: Implement model-grouped GSV phase**

Group only items with successful references by frozen `model_profile_id`. Resolve groups in stable profile-ID order, preserve utterance ordinal inside each group, submit GSV jobs and continue after failures. A profile switch/load failure marks remaining items for that profile failed and advances to the next profile.

- [ ] **Step 6: Implement composition and incomplete state**

If every spoken item has current GSV, compose in original utterance order using frozen pauses, persist timeline/final path and set `succeeded`. Otherwise set `generation_incomplete`. `resume()` retries only missing/failed items; `recompose()` uses current successful versions after explicit local regeneration.

- [ ] **Step 7: Write and pass recovery/failure tests**

Cover one reference failure, one GSV failure, one profile load failure, restart between phases, duplicate start, existing cache hit, narration disabled, no spoken utterances, and shutdown cancellation. Verify max GPU concurrency remains one.

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_director_generation.py tests/integration_cpu/test_director_generation.py tests/integration_cpu/test_director_generation_recovery.py -q`  
Expected: PASS.

- [ ] **Step 8: Commit Task 7**

```bash
git add src/voice_pipeline/core/director_generation.py src/voice_pipeline/api/director_routes.py src/voice_pipeline/api/app.py tests/unit/test_director_generation.py tests/integration_cpu/test_director_generation.py tests/integration_cpu/test_director_generation_recovery.py
git commit -m "feat: synthesize fault-isolated multi-role scripts"
```

### Task 8: Director WebUI

**Files:**
- Create: `src/voice_pipeline/webui/director.js`
- Create: `src/voice_pipeline/webui/director-dnd.js`
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Test: `tests/js/director-dnd.test.mjs`
- Test: `tests/contract/test_director_webui.py`
- Test: `tests/integration_cpu/test_director_webui_flow.py`

**Interfaces:**
- Adds primary tab `data-view="director"`.
- `director.js` exposes `initializeDirectorView`, `refreshDirectorProject`, and `stopDirectorActivity` to `app.js`.
- `director-dnd.js` remains DOM-independent for Node tests.

- [ ] **Step 1: Write failing pure JavaScript tests**

```javascript
test("bulk assignment keeps source order and expected revisions", () => {
  assert.deepEqual(buildAssignmentPatch(selected, roleId), {
    role_id: roleId,
    utterances: [
      {utterance_id: "u1", expected_revision: 2},
      {utterance_id: "u2", expected_revision: 4},
    ],
  });
});
```

Cover selected IDs, contiguous merge eligibility, narration filtering and stale response generation tokens.

- [ ] **Step 2: Run and verify RED**

Run: `node --test tests/js/director-dnd.test.mjs`  
Expected: module absent.

- [ ] **Step 3: Implement pure UI helpers**

No network or DOM access in `director-dnd.js`. Export deterministic selection, patch and filter helpers.

- [ ] **Step 4: Write failing shell contract test**

Assert the page contains director tab, project form, stage stepper, role palette, confidence filters, narration switch, translation review, preset mapping and generation progress containers; static assets use a new cache version.

- [ ] **Step 5: Implement director page structure and styling**

Use the approved layout: fixed role palette on the left and ordered utterance timeline on the right. Provide drag/drop plus dropdown fallback, multi-select, split/merge controls, filters and explicit review buttons. Preserve the current dark-console visual system and responsive behavior.

- [ ] **Step 6: Implement API state and race protection**

Use project/utterance request-generation tokens and `AbortController` where supported. Never replace a dirty translation editor with an SSE refresh. Store the selected director project in localStorage separately from chapter selection.

- [ ] **Step 7: Implement role preset and generation panels**

Support managed WAV picker/import, profile selection, base-audio playback, default speed, blocker list, start confirmation, source-ordered progress, failure filters, retry/resume and recompose.

- [ ] **Step 8: Run WebUI tests**

Run: `node --test tests/js/director-dnd.test.mjs`  
Run: `node --check src/voice_pipeline/webui/director.js`  
Run: `.venv/Scripts/python.exe -m pytest tests/contract/test_director_webui.py tests/integration_cpu/test_director_webui_flow.py -q`  
Expected: PASS.

- [ ] **Step 9: Commit Task 8**

```bash
git add src/voice_pipeline/webui/director.js src/voice_pipeline/webui/director-dnd.js src/voice_pipeline/webui/index.html src/voice_pipeline/webui/app.js src/voice_pipeline/webui/styles.css tests/js/director-dnd.test.mjs tests/contract/test_director_webui.py tests/integration_cpu/test_director_webui_flow.py
git commit -m "feat: add multi-role director workbench"
```

### Task 9: Recovery, Acceptance, Documentation and Release Verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-14-director-mode-design.md` only if implementation names differ, without changing approved behavior.
- Test: `tests/integration_cpu/test_director_end_to_end.py`
- Test: `tests/process/test_director_restart.py`

**Interfaces:**
- No new production interfaces; this task verifies the frozen feature end to end.

- [ ] **Step 1: Write the end-to-end fake-engine acceptance test**

Exercise: import mixed narration/dialogue/stage directions, disable narration, analyze, resolve low confidence, split one utterance, merge aliases, translate, edit target text, create two presets using two model profiles, start, inject one failure, continue, resume, compose, restart service, and confirm all persisted state and audit events.

- [ ] **Step 2: Run and verify RED against any uncovered behavior**

Run: `.venv/Scripts/python.exe -m pytest tests/integration_cpu/test_director_end_to_end.py tests/process/test_director_restart.py -q`  
Expected: any missing recovery or public-flow behavior fails before the final fixes.

- [ ] **Step 3: Make only acceptance-driven production fixes**

Each failure gets a minimal regression assertion before the corresponding fix. Do not expand scope beyond the approved design.

- [ ] **Step 4: Update user documentation**

README documents the five director stages, role preset storage, narration behavior, low-confidence gate, translation review, failure isolation and the fact that analysis does not use GPU. CHANGELOG adds the feature under the current unreleased section.

- [ ] **Step 5: Run complete static verification**

Run: `.venv/Scripts/python.exe -m ruff check src tests`  
Expected: PASS.  
Run: `.venv/Scripts/python.exe -m mypy src/voice_pipeline workers`  
Expected: PASS.  
Run: `node --check src/voice_pipeline/webui/app.js`  
Run: `node --check src/voice_pipeline/webui/director.js`  
Run: `node --test tests/js/*.test.mjs`  
Expected: PASS.

- [ ] **Step 6: Run complete CPU/process test suite**

Run: `.venv/Scripts/python.exe -m pytest -m "not gpu and not gpu_residency and not quality_model" -q`  
Expected: all selected tests pass with zero failures.

- [ ] **Step 7: Build and inspect the wheel**

Run: `uv build --wheel --out-dir dist-director`  
Expected: one wheel. Verify it contains `director.js`, `director-dnd.js`, migration `0004_director_mode.py`, and every new Python module.

- [ ] **Step 8: Install, restart and perform live loopback acceptance**

Install the wheel into `.venv-control`, verify there are no active jobs, restart via `scripts/stop.ps1` and `scripts/start.ps1`, then confirm:

```text
GET /api/v1/health -> ready
GET / -> director tab visible
POST /api/v1/director-projects -> 201
POST /api/v1/director-projects/{id}/analyze -> 202
```

Use the in-app browser to complete a fake or configured real LLM analysis and confirm no console errors. Do not trigger real GPU generation unless all selected role preset assets are present and healthy.

- [ ] **Step 9: Commit Task 9**

```bash
git add README.md CHANGELOG.md docs/superpowers/specs/2026-08-14-director-mode-design.md tests/integration_cpu/test_director_end_to_end.py tests/process/test_director_restart.py
git commit -m "docs: complete director mode delivery"
```

## Plan Self-Review Result

- Every approved design section maps to at least one task.
- Existing single-role APIs remain untouched except shared LLM settings gaining a backward-compatible default.
- Source coverage, OCC, late-result protection, managed audio, model snapshots, two-phase scheduling, failure isolation, recovery, drag/drop fallback and full verification have explicit tests.
- No production task depends on an interface that is not introduced in an earlier task.
- The implementation is one coherent feature but is split at independently reviewable storage, LLM, preset, API, generation and UI boundaries.
