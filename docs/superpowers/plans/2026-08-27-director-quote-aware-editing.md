# Director Quote-Aware Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split quoted dialogue from surrounding narration deterministically and let users edit a separate working script that becomes the input to all later translation and dubbing stages.

**Architecture:** Extend stable local analysis units with a server-owned quote context, then enforce only the structural classifications that can be proven locally. Add a migrated `working_text` column while keeping source ranges immutable; translation reads the working text and the WebUI exposes explicit draft/save controls plus a read-only source audit view.

**Tech Stack:** Python 3.11, Pydantic v2, SQLAlchemy async, Alembic/SQLite, FastAPI, vanilla ES modules, pytest, Node-based frontend contract tests.

## Global Constraints

- Preserve every original source character and exact absolute offset.
- Analysis units remain contiguous, stable, unique, and at most 160 characters.
- Support balanced `“”`, `「」`, `『』`, and `""`; unmatched quotes fall back losslessly.
- `source_text` remains immutable; `working_text` is the only role-review edit surface.
- `working_text` edits are accepted only in `role_review` with optimistic revision checks.
- Translation must consume `working_text`; generation continues to consume reviewed `synthesis_text`.
- Analysis cache contract is exactly fingerprint `runtime-director-quote-units-v3`, prompt `director-analysis-quote-units-v3`, schema `3`.
- Git commits use `yjsnpi1145 <259851991+yjsnpi1145@users.noreply.github.com>`.
- Do not run `tests/process/test_start_stop_scripts.py` against the live local service.

---

### Task 1: Quote-aware stable analysis units

**Files:**
- Modify: `src/voice_pipeline/models/director_llm.py`
- Modify: `src/voice_pipeline/modules/llm/script_chunking.py`
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Test: `tests/unit/test_director_llm_stages.py`
- Test: `tests/unit/test_llm_client.py`

**Interfaces:**
- Produces: `ScriptAnalysisUnit.context: Literal["general", "quoted_dialogue", "quote_bridge_narration"]`.
- Produces: `build_analysis_units(chunk, max_unit_chars=160) -> tuple[ScriptAnalysisUnit, ...]` with quote-aware ranges.
- Produces: `materialize_unit_analysis(...)` enforcing locally proven classifications.

- [ ] **Step 1: Write failing quote-boundary tests**

Add tests asserting the exact three-unit split and context sequence:

```python
source = "“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”"
units = build_analysis_units(ScriptChunk(
    chunk_id="quotes", source_start=0, source_end=len(source), source_text=source
))
assert [(item.source_text, item.context) for item in units] == [
    ("“我的初吻……”", "quoted_dialogue"),
    ("她慌乱地摆弄着手指，目光四处乱飘，", "quote_bridge_narration"),
    ("“祥子，为什么——”", "quoted_dialogue"),
]
```

Parametrize `「日文」`, `『二重』`, and `"English"`; add an unmatched opener case and a 200-character quoted span. In every case assert exact reconstruction, adjacent offsets, and the 160-character ceiling.

- [ ] **Step 2: Run the quote tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_llm_stages.py -q
```

Expected: FAIL because `ScriptAnalysisUnit` has no `context` and the mixed sentence is one unit.

- [ ] **Step 3: Implement the quote scanner and range segmenter**

Add the context field:

```python
AnalysisUnitContext = Literal["general", "quoted_dialogue", "quote_bridge_narration"]

class ScriptAnalysisUnit(StrictModel):
    unit_id: NonBlankText
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_text: str
    context: AnalysisUnitContext = "general"
```

In `script_chunking.py`, scan balanced outer spans with `_QUOTE_CLOSERS = {"“": "”", "「": "」", "『": "』"}` plus a toggled ASCII quote. Convert those spans into `(start, end, context)` ranges; only a non-empty, no-newline gap between adjacent quoted spans receives `quote_bridge_narration`. Segment each range at safe boundaries/max length without normalizing characters, then construct stable IDs in final order.

- [ ] **Step 4: Write failing materialization constraint tests**

Return deliberately wrong LLM annotations and assert local overrides:

```python
assert [row.kind for row in result.utterances] == ["dialogue", "narration", "dialogue"]
assert result.utterances[1].temporary_role_name is None
assert result.utterances[1].role_aliases == ()
assert all(row.speak_enabled for row in result.utterances)
```

Also assert a `general` unit keeps the LLM-provided classification.

- [ ] **Step 5: Run materialization tests and verify RED**

Run the targeted test file again. Expected: the wrong classifications are currently passed through.

- [ ] **Step 6: Enforce contexts and include them in the LLM request**

In `materialize_unit_analysis`, compute the final fields from the unit context before constructing `AnalyzedUtterance`. In `OpenAiDirectorClient.analyze_script_chunk`, send:

```python
{
    "unit_id": unit.unit_id,
    "source_text": unit.source_text,
    "context": unit.context,
}
```

Update the prompt to explain that quoted units and bridge narration are local structural constraints while still requiring a classification/role candidate for every ID.

- [ ] **Step 7: Update and run the OpenAI client contract test**

Change the expected request keys to `{"unit_id", "source_text", "context"}` and assert the system prompt names both contexts. Run:

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_llm_stages.py tests/unit/test_llm_client.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```powershell
git add src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/script_chunking.py src/voice_pipeline/modules/llm/client.py tests/unit/test_director_llm_stages.py tests/unit/test_llm_client.py
git commit -m "fix: split quoted director dialogue deterministically"
```

---

### Task 2: Persist an editable working text without mutating source slices

**Files:**
- Modify: `src/voice_pipeline/storage/orm.py`
- Create: `src/voice_pipeline/storage/migrations/versions/0005_director_working_text.py`
- Modify: `src/voice_pipeline/models/director.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Test: `tests/unit/test_director_models.py`
- Test: `tests/integration_cpu/test_director_migration.py`
- Test: `tests/integration_cpu/test_director_store.py`

**Interfaces:**
- Produces: `DirectorUtteranceRecord.working_text: PreservedNonBlankText`.
- Produces: `DirectorUtterancePatch.working_text: PreservedNonBlankText | None`.
- Consumes: existing `DirectorStore.patch_utterance(..., expected_revision, **changes)`.

- [ ] **Step 1: Write failing model and migration tests**

Extend `valid_utterance()` with `working_text="你好"`. Assert patch parsing preserves leading/trailing whitespace but rejects an all-whitespace value. Update migration expectation to `0005_director_working_text` and assert:

```python
columns = {
    str(row[1]) for row in await session.execute(text("PRAGMA table_info(director_utterances)"))
}
assert "working_text" in columns
```

Add an upgrade/backfill test that creates a 0004 database row with `source_text`, upgrades to head, and asserts `working_text == source_text`.

- [ ] **Step 2: Run model/migration tests and verify RED**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py -q
```

Expected: FAIL because the field and revision do not exist.

- [ ] **Step 3: Add the column, migration, and record mapping**

Add `Column("working_text", Text, nullable=False)` after `source_text` in ORM metadata. Migration `0005_director_working_text` must inspect the table, add a nullable text column when absent, execute `UPDATE director_utterances SET working_text = source_text WHERE working_text IS NULL`, then use `batch_alter_table` to make it non-null. Downgrade removes it with a batch operation.

Add `working_text` to the record and patch models. Initialize it in `publish_analysis` and map it in `_utterance`.

- [ ] **Step 4: Write failing store behavior tests**

Assert that analysis publication initializes working text, PATCH preserves `source_text`, rejects changes outside `role_review`, increments revisions, and clears downstream fields. Assert split succeeds only when `working_text == source_text`; assert merge produces `left.working_text + right.working_text`.

- [ ] **Step 5: Run store tests and verify RED**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/integration_cpu/test_director_store.py -q
```

Expected: FAIL because store rows neither save nor validate `working_text`.

- [ ] **Step 6: Implement guarded edits, split, and merge semantics**

Allow `working_text` in `patch_utterance`. Before updating, load the project and require `role_review` for this field. When present, set:

```python
values.update(
    synthesis_text=None,
    ref_text_cn=None,
    emotion_vector_json=None,
    task_id=None,
    segment_id=None,
    reference_version_id=None,
    gsv_version_id=None,
)
```

Treat `working_text`, `role_id`, `speak_enabled`, and `role_confirmed` as role-review changes so `_touch_project(..., role_change=True)` keeps the project at `role_review`. In split, reject edited working text with `INVALID_INPUT`; otherwise split both source and working text. In merge, concatenate each text field independently and clear derived data.

- [ ] **Step 7: Run model/store/migration tests**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```powershell
git add src/voice_pipeline/storage/orm.py src/voice_pipeline/storage/migrations/versions/0005_director_working_text.py src/voice_pipeline/models/director.py src/voice_pipeline/storage/director_store.py tests/unit/test_director_models.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py
git commit -m "feat: persist editable director working text"
```

---

### Task 3: Feed edited text through translation and invalidate old analysis caches

**Files:**
- Modify: `src/voice_pipeline/core/director_analysis.py`
- Test: `tests/integration_cpu/test_director_analysis.py`

**Interfaces:**
- Consumes: `DirectorUtteranceRecord.working_text` from Task 2.
- Produces: `TranslationInput.source_text == DirectorUtteranceRecord.working_text`.
- Produces: v3 cache metadata constants.

- [ ] **Step 1: Write failing translation-input and cache tests**

Create a `CapturingDirector(FakeDirector)` that stores the tuple received by `translate_utterances`. Analyze a project, PATCH its spoken utterance to `working_text="修改后的台词。"`, confirm roles, translate, and assert the captured `source_text` is exactly that value while stored `source_text` is unchanged.

Update the cache test to write v2 metadata, assert a v3 lookup misses, then write and load the exact v3 metadata.

- [ ] **Step 2: Run analysis integration tests and verify RED**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/integration_cpu/test_director_analysis.py -q
```

Expected: translation capture contains original `source_text`, and cache expectations differ.

- [ ] **Step 3: Switch translation input and cache constants**

Change the constants to the exact v3 values in Global Constraints. Construct `TranslationInput(source_text=item.working_text, ...)`.

- [ ] **Step 4: Run analysis integration tests**

Run the same command. Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```powershell
git add src/voice_pipeline/core/director_analysis.py tests/integration_cpu/test_director_analysis.py
git commit -m "feat: translate edited director working text"
```

---

### Task 4: Expose working-text editing and source audit controls in WebUI

**Files:**
- Create: `src/voice_pipeline/webui/director-working-text.js`
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Create: `tests/unit/test_director_working_text_js.py`
- Modify: `tests/contract/test_director_api.py`

**Interfaces:**
- Produces: pure helpers `isWorkingTextDirty(utterance, draft)`, `canSplitWorkingText(utterance)`, and `hasUnsavedDirectorDrafts(state)`.
- Consumes: PATCH `/api/v1/director-utterances/{utterance_id}` with `{expected_revision, working_text}`.

- [ ] **Step 1: Write failing JavaScript helper test**

Use the existing Node subprocess pattern to assert:

```javascript
isWorkingTextDirty({working_text: '原文'}, '修改') === true
canSplitWorkingText({source_text: '原文', working_text: '原文'}) === true
canSplitWorkingText({source_text: '原文', working_text: '修改'}) === false
hasUnsavedDirectorDrafts({dirtyWorkingTexts: new Map([['u1', '修改']]), dirtyTranslations: new Map()}) === true
```

- [ ] **Step 2: Run the helper test and verify RED**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_working_text_js.py -q
```

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement helpers and role-review editor**

Add `dirtyWorkingTexts: new Map()` to state. In each card, make the primary textarea show the draft or `utterance.working_text`; attach `oninput` to store/remove the draft. Add an explicit “保存配音文本” button that PATCHes `working_text`, clears the draft, and refreshes the project.

Create `<details>` with summary “查看原始切片”; inside place a read-only textarea bound to `source_text`. The split button reads selection from this original textarea, is labelled “在原文光标处拆分”, and is disabled when `working_text !== source_text`.

Use `hasUnsavedDirectorDrafts` when switching projects and before the role-confirm action; block the action with a clear notification instead of silently discarding drafts.

- [ ] **Step 4: Add API contract assertions**

In the director API test, assert every utterance returns `working_text == source_text`; PATCH one utterance in `role_review`, assert HTTP 200, unchanged source, updated working text, and stale revision HTTP 409.

- [ ] **Step 5: Add responsive styles**

Style the editable working textarea, dirty state, save button, and source `<details>` consistently with the dark console. Preserve the one-column mobile breakpoint.

- [ ] **Step 6: Run frontend and API tests**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_working_text_js.py tests/contract/test_director_api.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 4**

```powershell
git add src/voice_pipeline/webui/director-working-text.js src/voice_pipeline/webui/director.js src/voice_pipeline/webui/styles.css tests/unit/test_director_working_text_js.py tests/contract/test_director_api.py
git commit -m "feat: edit director dubbing text in role review"
```

---

### Task 5: End-to-end regression and quality gates

**Files:**
- Test: `tests/integration_cpu/test_director_end_to_end.py`

**Interfaces:**
- Verifies the complete approved behavior; introduces no new production interface.

- [ ] **Step 1: Add an end-to-end edited-text assertion**

Extend the fake director flow so a role-review working-text edit is followed by translation and generation. Assert the created segment's synthesis text matches the result derived from the edited input and the utterance retains its original source slice.

- [ ] **Step 2: Run the focused director suite**

```powershell
$env:PYTHONPATH="$PWD\src"
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest tests/unit/test_director_models.py tests/unit/test_director_llm_stages.py tests/unit/test_llm_client.py tests/unit/test_director_working_text_js.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py tests/integration_cpu/test_director_analysis.py tests/integration_cpu/test_director_end_to_end.py tests/contract/test_director_api.py -q
```

Expected: PASS with zero failures.

- [ ] **Step 3: Run static checks**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m ruff check src tests
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m mypy src/voice_pipeline workers
```

Expected: both exit 0.

- [ ] **Step 4: Run the non-destructive full suite**

```powershell
& 'D:\TTSsystem\.venv\Scripts\python.exe' -m pytest -m "not gpu and not gpu_residency and not quality_model and not process and not crash_recovery" -q
```

Expected: zero failures. Do not run the fixed-port start/stop test locally while the service is live.

- [ ] **Step 5: Build and inspect the wheel**

```powershell
uv build --wheel
& 'D:\TTSsystem\.venv\Scripts\python.exe' -c "import zipfile,glob; p=glob.glob('dist/*.whl')[-1]; z=zipfile.ZipFile(p); assert any(n.endswith('director-working-text.js') for n in z.namelist()); print(p)"
```

Expected: build exits 0 and the WebUI helper is packaged.

- [ ] **Step 6: Review the complete diff and commit any test-only additions**

```powershell
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
```

If Task 5 changed tests, commit them with:

```powershell
git add tests/integration_cpu/test_director_end_to_end.py
git commit -m "test: cover edited director text end to end"
```

---

### Task 6: Publish, merge, deploy, and runtime acceptance

**Files:**
- No new source files expected.

**Interfaces:**
- Produces: merged GitHub PR and updated local service at `http://127.0.0.1:8765/`.

- [ ] **Step 1: Push and create the PR**

```powershell
git push -u origin codex/director-quote-aware-editing
gh pr create --base main --head codex/director-quote-aware-editing --title "feat: add quote-aware director script editing" --body-file docs/superpowers/specs/2026-08-27-director-quote-aware-editing-design.md
```

- [ ] **Step 2: Verify GitHub checks and merge**

```powershell
gh pr checks --watch
gh pr merge --squash --delete-branch
git fetch origin
```

Expected: all required checks pass and `origin/main` contains the squash commit.

- [ ] **Step 3: Confirm no active local work before restart**

Query `/api/v1/health` and verify `active_analysis == 0` and `active_generation == 0`. If either is nonzero, wait rather than interrupting the task.

- [ ] **Step 4: Build merged main and restart all managed services**

Build from the merged revision, stop with `scripts/stop.ps1`, force-install the wheel into `D:\TTSsystem\.venv-control`, then start with `scripts/start.ps1` using the existing real-mode configuration.

- [ ] **Step 5: Runtime acceptance**

Verify `/api/v1/health`, `/`, and director static assets return 200. Open the director page and confirm the role-review card has an editable “配音文本”, a save control, a collapsible original source view, and the original-source split control. Record the deployed commit and service PID.
