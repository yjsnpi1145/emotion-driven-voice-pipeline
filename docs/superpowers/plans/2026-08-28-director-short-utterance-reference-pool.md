# Director Short-Utterance Reference Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one- and two-character Director utterances reliably synthesize by reusing durable, role-voice-specific reference audio selected from eight emotion buckets.

**Architecture:** Add a pure short-reference policy module, a small persistent pool ledger over the existing content-addressed audio cache, and a Director-only reference job override that keeps reviewed utterance fields immutable while binding GSV to the actual pooled prompt. Director generation uses the pool directly for short references and as a final fallback after normal LLM correction; the existing durable job queue, quality policy, artifact versions and GSV model grouping remain authoritative.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy Core, Alembic, SQLite, asyncio, vanilla JavaScript/CSS, pytest.

## Global Constraints

- A short reference is one or two effective Unicode letters/numbers/CJK ideographs after whitespace, quote and punctuation removal.
- A non-calm emotion wins only when its value is at least `0.15` and leads the second-highest value by at least `0.05`; otherwise use calm.
- Non-calm canonical vectors use `0.60` on the selected dimension and `0.20` on calm; calm uses `0.80` on calm and zero elsewhere.
- Do not modify reviewed `source_text`, `working_text`, `synthesis_text`, continuous `emotion_vector`, speed, pause or target seed.
- A pooled WAV must remain bound to the exact pool `prompt_text`, canonical vector, base-voice SHA-256 and IndexTTS2 fingerprint used to create it.
- Reference audio must pass the existing closed `3.0..10.0` duration window and VAD policy; ASR-text scoring remains independently configurable.
- Try three deterministic seeds per pool revision, then degrade a non-calm bucket to calm; calm exhaustion fails only that utterance.
- Reuse `generation_jobs`, the single-GPU queue, `cache_entries`, `artifact_blobs`, artifact versions and quality cache; do not add a second inference queue or WAV repository.
- Use migration revision `0008_director_reference_pool` with down revision `0007_director_role_dubbing`.
- Every behavior change follows RED → GREEN → regression verification before commit.

---

## File Structure

- Create `src/voice_pipeline/core/director_reference_pool.py`: pure normalization, emotion selection, templates, canonical vectors, family keys and retry-seed derivation.
- Create `src/voice_pipeline/storage/reference_pool_store.py`: atomic pool-attempt ledger and ready-entry lookup.
- Create `src/voice_pipeline/storage/migrations/versions/0008_director_reference_pool.py`: pool table and Director generation-item metadata.
- Modify `src/voice_pipeline/storage/orm.py`: SQLAlchemy table/columns matching migration 0008.
- Modify `src/voice_pipeline/models/director.py`: pool record, mode and generation progress fields.
- Modify `src/voice_pipeline/models/persistence.py`: internal immutable `ReferenceInputOverride`.
- Modify `src/voice_pipeline/core/segment_job_service.py`: freeze an optional Director-only reference override.
- Modify `src/voice_pipeline/storage/director_store.py`: persist pool metadata on generation items.
- Modify `src/voice_pipeline/core/director_generation.py`: choose/build/reuse/degrade pool references and preserve exact job errors.
- Modify `src/voice_pipeline/api/app.py`: construct and inject `ReferencePoolStore`.
- Modify `src/voice_pipeline/api/director_routes.py`: enrich progress with pool data and expose force-rebuild action.
- Modify `src/voice_pipeline/webui/director.js`: render pool/degradation badges, actual prompt and rebuild control.
- Modify `src/voice_pipeline/webui/styles.css`: compact pool status treatment.
- Create `tests/unit/test_director_reference_pool.py`: pure policy tests.
- Modify `tests/integration_cpu/test_director_migration.py`: migration 0008 coverage.
- Create `tests/integration_cpu/test_director_reference_pool_store.py`: persistent claim/ready/failure semantics.
- Modify `tests/integration_cpu/test_director_end_to_end.py`: pool integration, fallback, cache reuse, binding and field preservation.
- Modify `tests/contract/test_director_api.py` or the existing Director contract module selected by repository discovery: progress and rebuild API contract.
- Modify `tests/unit/test_director_dnd_js.py` or add `tests/unit/test_director_reference_pool_js.py`: browser rendering source contract.

---

### Task 1: Pure short-reference and emotion policy

**Files:**
- Create: `src/voice_pipeline/core/director_reference_pool.py`
- Create: `tests/unit/test_director_reference_pool.py`

**Interfaces:**
- Produces: `EmotionBucket`, `PoolReferenceSpec`, `effective_reference_text(text)`, `is_short_reference(text)`, `select_emotion_bucket(vector)`, `reference_spec(bucket, revision, attempt)`, and `build_pool_family_key(...)`.
- Consumes: existing `EmotionVector`, `EngineFingerprint`, `OutputAudioSpec` and canonical JSON/hash helpers or local deterministic equivalents.

- [ ] **Step 1: Write failing policy tests**

```python
def test_short_reference_counts_unicode_content_not_punctuation():
    assert is_short_reference(' “嗯？” ')
    assert is_short_reference('「砰！」')
    assert not is_short_reference('“嗯，Your Majesty”')
    assert not is_short_reference('为什么')


def test_emotion_bucket_requires_strength_and_margin():
    assert select_emotion_bucket((0.20, 0.01, 0, 0, 0, 0, 0, 0)) == 'joy'
    assert select_emotion_bucket((0.14, 0, 0, 0, 0, 0, 0, 0)) == 'calm'
    assert select_emotion_bucket((0.20, 0.16, 0, 0, 0, 0, 0, 0)) == 'calm'
    assert select_emotion_bucket((0.10,) * 8) == 'calm'


def test_reference_spec_uses_exact_template_vector_and_new_revision_seed():
    first = reference_spec('surprise', revision=0, attempt=0)
    rebuilt = reference_spec('surprise', revision=1, attempt=0)
    assert first.prompt_text == '我完全没有想到，事情竟然会变成这样。'
    assert first.emotion_vector == (0, 0, 0, 0, 0, 0, 0.6, 0.2)
    assert first.seed != rebuilt.seed
```

- [ ] **Step 2: Run RED**

Run: `.venv-control\Scripts\python.exe -m pytest tests/unit/test_director_reference_pool.py -q`  
Expected: collection fails because `voice_pipeline.core.director_reference_pool` does not exist.

- [ ] **Step 3: Implement the pure module**

```python
EmotionBucket = Literal['joy', 'anger', 'sadness', 'fear', 'disgust', 'melancholy', 'surprise', 'calm']
POOL_TEMPLATE_VERSION = 1
POOL_RETRY_SEEDS = (1234, 2345, 3456)

@dataclass(frozen=True)
class PoolReferenceSpec:
    bucket: EmotionBucket
    template_version: int
    prompt_text: str
    emotion_vector: tuple[float, float, float, float, float, float, float, float]
    revision: int
    attempt: int
    seed: int

def is_short_reference(text: str) -> bool:
    return 1 <= len(effective_reference_text(text)) <= 2

def select_emotion_bucket(vector: Sequence[float]) -> EmotionBucket:
    # validate length eight, pick calm for ties/weak margins, otherwise map argmax
```

Implement all eight approved templates and derive retry seeds as `POOL_RETRY_SEEDS[attempt] + revision * 100_003`.

- [ ] **Step 4: Run GREEN and unit regression**

Run: `.venv-control\Scripts\python.exe -m pytest tests/unit/test_director_reference_pool.py tests/unit/test_director_models.py -q`  
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/voice_pipeline/core/director_reference_pool.py tests/unit/test_director_reference_pool.py
git commit -m "feat: define Director short reference policy"
```

---

### Task 2: Persistent pool ledger and generation metadata

**Files:**
- Create: `src/voice_pipeline/storage/migrations/versions/0008_director_reference_pool.py`
- Modify: `src/voice_pipeline/storage/orm.py`
- Create: `src/voice_pipeline/storage/reference_pool_store.py`
- Modify: `src/voice_pipeline/models/director.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Modify: `tests/integration_cpu/test_director_migration.py`
- Create: `tests/integration_cpu/test_director_reference_pool_store.py`

**Interfaces:**
- Produces: `DirectorReferencePoolEntry`, `ReferencePoolStore.begin_attempt(...)`, `mark_ready(...)`, `mark_failed(...)`, `latest_ready(family_key)`, `next_revision(family_key)`, and enriched `DirectorGenerationItemRecord`.
- Consumes: `PoolReferenceSpec` and existing `Database` session patterns.

- [ ] **Step 1: Write migration and store RED tests**

Assert migration head is `0008_director_reference_pool`, all new columns exist, duplicate `(family_key, revision, attempt)` is rejected, the latest ready revision wins, failed attempts are retained, and a generation item round-trips:

```python
assert item.reference_mode == 'pooled'
assert item.reference_emotion_bucket == 'surprise'
assert item.reference_degraded_from is None
assert item.reference_pool_entry_id == entry.entry_id
```

- [ ] **Step 2: Run RED**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_reference_pool_store.py -q`  
Expected: missing migration/table/model failures.

- [ ] **Step 3: Add migration and ORM definitions**

Create `director_reference_pool_entries` with immutable identity/input fields plus mutable terminal fields:

```text
entry_id, family_key, revision, attempt, status,
base_voice_sha256, emotion_bucket, template_version, prompt_text,
emotion_vector_json, seed, engine_fingerprint_json, output_spec_json,
reference_job_id, reference_version_id, blob_sha256,
quality_result_json, error_json, degraded_from,
created_at_utc, updated_at_utc
UNIQUE(family_key, revision, attempt)
```

Add nullable generation-item columns `reference_mode`, `reference_pool_entry_id`, `reference_emotion_bucket`, `reference_degraded_from`; existing rows deserialize as `independent` with null pool fields.

- [ ] **Step 4: Implement store methods and model conversion**

Use one write transaction per state transition. `mark_ready` and `mark_failed` require `status='building'` in their update predicate and raise `VERSION_CONFLICT` on stale writers. `latest_ready` orders by revision descending, then attempt descending.

- [ ] **Step 5: Run GREEN and storage regression**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_reference_pool_store.py tests/integration_cpu/test_director_store.py -q`  
Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/voice_pipeline/storage/migrations/versions/0008_director_reference_pool.py src/voice_pipeline/storage/orm.py src/voice_pipeline/storage/reference_pool_store.py src/voice_pipeline/models/director.py src/voice_pipeline/storage/director_store.py tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_reference_pool_store.py
git commit -m "feat: persist Director reference pool entries"
```

---

### Task 3: Freeze pooled reference inputs without changing reviewed fields

**Files:**
- Modify: `src/voice_pipeline/models/persistence.py`
- Modify: `src/voice_pipeline/core/segment_job_service.py`
- Modify: `tests/integration_cpu/test_segment_bound_jobs.py`

**Interfaces:**
- Produces: immutable `ReferenceInputOverride(ref_text_cn, emotion_vector, seed)` and `SegmentJobService.submit_reference(..., reference_override=None)`.
- Consumes: the existing segment snapshot, base voice path and durable job store.

- [ ] **Step 1: Write failing freeze tests**

```python
override = ReferenceInputOverride(
    ref_text_cn='我完全没有想到，事情竟然会变成这样。',
    emotion_vector=(0, 0, 0, 0, 0, 0, 0.6, 0.2),
    seed=1234,
)
context = await service.submit_reference(segment_id, request, reference_override=override)
job = await jobs.get(context.job_id)
assert job.request_snapshot['ref_text_cn'] == override.ref_text_cn
assert (await segments.get_segment(segment_id)).ref_text_cn == '嗯？'
assert (await segments.get_segment(segment_id)).current_emotion_vector == original_vector
```

- [ ] **Step 2: Run RED**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_segment_bound_jobs.py -q`  
Expected: `ReferenceInputOverride` or keyword argument is missing.

- [ ] **Step 3: Implement the optional override**

When absent, preserve exact existing behavior. When present, build `ReferenceJobRequest` from the override while freezing the same segment revision and output spec. Never expose the override in public segment regeneration request models.

- [ ] **Step 4: Run GREEN and persistence contract regression**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_segment_bound_jobs.py tests/contract/test_foundation_api.py -q`  
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/voice_pipeline/models/persistence.py src/voice_pipeline/core/segment_job_service.py tests/integration_cpu/test_segment_bound_jobs.py
git commit -m "feat: freeze pooled Director reference inputs"
```

---

### Task 4: Integrate pool build, cache reuse, retry and calm degradation

**Files:**
- Modify: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/api/app.py`
- Modify: `tests/integration_cpu/test_director_end_to_end.py`

**Interfaces:**
- Consumes: policy functions, `ReferencePoolStore`, `ReferenceInputOverride`, durable job notifications and exact persisted job errors.
- Produces: `_prepare_reference(...)` returning the successful job/version plus pool metadata, and `force_rebuild_pool_reference(generation_id, utterance_id)` for Task 5.

- [ ] **Step 1: Write RED integration tests**

Cover these separately:

1. “嗯？” uses surprise pool and does not call `correct_reference`.
2. A three-character reference whose probe exhausts corrections falls back to its pool bucket.
3. Three identical short references cause one IndexTTS execution and later durable jobs hit the existing cache.
4. A failed surprise seed sequence degrades to calm and records both buckets.
5. Calm exhaustion fails only the current item.
6. GSV binding uses the pool template while target text and reviewed utterance fields remain unchanged.
7. A persisted `QUALITY_VAD_FAILED` remains that exact code instead of becoming `ENGINE_UNAVAILABLE`.

- [ ] **Step 2: Run RED**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_director_end_to_end.py -q`  
Expected: short references still use `ReferenceTextDirector` and fail the new assertions.

- [ ] **Step 3: Add exact job-error reconstruction**

In `_await_job`, validate `record.error['code']` against `ErrorCode`; reconstruct `PipelineError(code, stage, message, retryable, details)` when possible and use `ENGINE_UNAVAILABLE` only for malformed legacy errors.

- [ ] **Step 4: Implement pooled preparation**

Replace the direct resolve/submit block with:

```python
prepared = await self._prepare_reference(
    generation_id=generation_id,
    segment=segment,
    utterance=utterance,
    base_voice=self._presets.audio_path(preset),
)
```

The helper must acquire an asyncio lock by family key, consult the persistent latest-ready entry, submit a cache-backed reference job for every target segment, build up to three attempts when absent, persist every attempt, degrade to calm once, attach the resulting reference version, and update generation-item metadata. Catch only `REFERENCE_DURATION_OUT_OF_RANGE`, `REFERENCE_DURATION_INVALID`, `QUALITY_VAD_FAILED` and other reference-quality errors for retries; propagate cancellation and unrelated storage/configuration errors.

- [ ] **Step 5: Wire the store through the control plane**

Create one `ReferencePoolStore` from the existing `Database` in `ControlPlane`, inject it into `DirectorGenerationService`, and reuse that instance for routes.

- [ ] **Step 6: Run GREEN and Director regressions**

Run: `.venv-control\Scripts\python.exe -m pytest tests/integration_cpu/test_director_end_to_end.py tests/integration_cpu/test_director_store.py tests/unit/test_llm_reference_correction.py -q`  
Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src/voice_pipeline/core/director_generation.py src/voice_pipeline/api/app.py tests/integration_cpu/test_director_end_to_end.py
git commit -m "feat: use emotion reference pool for short Director lines"
```

---

### Task 5: Progress API, WebUI state and explicit pool rebuild

**Files:**
- Modify: `src/voice_pipeline/api/director_routes.py`
- Modify: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Create or modify: `tests/contract/test_director_api.py`
- Create: `tests/unit/test_director_reference_pool_js.py`

**Interfaces:**
- Produces: progress item `reference_pool` detail and `POST /api/v1/director-generations/{generation_id}/utterances/{utterance_id}/rebuild-pooled-reference`.
- Consumes: `force_rebuild_pool_reference(...)` and current generation-item optimistic state.

- [ ] **Step 1: Write RED API and JS tests**

Assert progress includes:

```json
{
  "reference_mode": "pooled",
  "reference_emotion_bucket": "surprise",
  "reference_degraded_from": null,
  "reference_pool": {
    "entry_id": "...",
    "prompt_text": "我完全没有想到，事情竟然会变成这样。",
    "revision": 0,
    "attempt": 0,
    "status": "ready"
  }
}
```

Assert the JS source renders `情绪池：惊讶`, `已降级：惊讶 → 平静`, actual prompt text and a rebuild button only for pooled items.

- [ ] **Step 2: Run RED**

Run: `.venv-control\Scripts\python.exe -m pytest tests/contract/test_director_api.py tests/unit/test_director_reference_pool_js.py -q`  
Expected: missing progress fields/route/rendering.

- [ ] **Step 3: Enrich progress and add rebuild endpoint**

The endpoint returns `202`, rejects non-pooled items with `409`, refuses while that item is already running, increments the pool revision, keeps the current reference version active until the new version succeeds, and schedules reference regeneration for that utterance. On success it updates the item reference binding; it does not silently regenerate GSV.

- [ ] **Step 4: Render compact pool details**

Add a badge and collapsible actual-prompt detail to each generation row. Add “重建池参考” beside pooled entries and refresh progress after acceptance. Use textContent/DOM methods for untrusted text and the existing `api`, `busy` and `notify` helpers.

- [ ] **Step 5: Run GREEN plus WebUI/contract regressions**

Run: `.venv-control\Scripts\python.exe -m pytest tests/contract/test_director_api.py tests/unit/test_director_reference_pool_js.py tests/integration_cpu/test_webui_workbench.py -q`  
Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src/voice_pipeline/api/director_routes.py src/voice_pipeline/core/director_generation.py src/voice_pipeline/webui/director.js src/voice_pipeline/webui/styles.css tests/contract/test_director_api.py tests/unit/test_director_reference_pool_js.py
git commit -m "feat: expose Director pooled reference status"
```

---

### Task 6: Full verification, live migration and GPU smoke test

**Files:**
- Modify if needed: `docs/batch-1-gpu-runbook.md`
- Modify if needed: focused regression files only when a newly observed defect first has a failing test.

**Interfaces:**
- Consumes: the complete feature.
- Produces: migration evidence, automated-suite evidence, live-service evidence and a clean feature branch.

- [ ] **Step 1: Run static and focused verification**

```powershell
.venv-control\Scripts\python.exe -m ruff check src tests
.venv-control\Scripts\python.exe -m pytest tests/unit/test_director_reference_pool.py tests/integration_cpu/test_director_reference_pool_store.py tests/integration_cpu/test_director_end_to_end.py tests/contract/test_director_api.py tests/unit/test_director_reference_pool_js.py -q
```

Expected: zero lint errors and all focused tests pass.

- [ ] **Step 2: Run the complete non-GPU suite**

Run: `.venv-control\Scripts\python.exe -m pytest -m "not gpu" -q`  
Expected: all tests pass with no unexpected warnings.

- [ ] **Step 3: Verify migration against a copy of live state**

Copy `runtime/state/pipeline.sqlite3` to a temporary test location, run the normal Alembic upgrade to `head`, then assert `PRAGMA quick_check` is `ok` and revision is `0008_director_reference_pool`. Never mutate the only live database copy for this dry run.

- [ ] **Step 4: Deploy and run real GPU smoke cases**

Stop services through the supported shutdown path, build/install the merged wheel if the project startup process requires it, start `scripts/start.ps1`, and verify `/api/v1/health` reports `ready` with Alembic head 0008. Run or create a small Director project using two role presets and “砰 / 嗯。 / 嗯？” for Chinese and Japanese target text. Confirm all pool references are 3.0～10.0 seconds, VAD passes, GSV outputs play, prompts match manifests and role voices do not cross.

- [ ] **Step 5: Run full regression after live smoke fixes**

Run: `.venv-control\Scripts\python.exe -m pytest -q`  
Expected: the complete suite passes.

- [ ] **Step 6: Commit any runbook-only change**

```powershell
git add docs/batch-1-gpu-runbook.md
git commit -m "docs: add short reference pool GPU checks"
```

Skip this commit when the runbook already contains all required commands and no file changed.

---

## Plan Self-Review

- Spec coverage: trigger, eight buckets, exact thresholds/vectors, template versioning, persistent pool ledger, content-addressed reuse, three seeds, calm degradation, immutable reviewed fields, exact GSV binding, UI visibility, rebuild semantics, exact errors, restart behavior and GPU acceptance are each assigned to a task.
- Placeholder scan: no TBD/TODO/“similar to” steps remain; every implementation task names exact files, interfaces, RED command, GREEN command and commit boundary.
- Type consistency: `PoolReferenceSpec`, `DirectorReferencePoolEntry`, `ReferenceInputOverride`, generation-item pool fields and `force_rebuild_pool_reference` retain the same names from their defining tasks through API/UI tasks.
