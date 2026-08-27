# Director Stable Slice Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove LLM-generated source indices and copied source text from Director Mode analysis so every analyzed utterance is materialized from deterministic local source slices.

**Architecture:** Split each existing `ScriptChunk` locally into stable, contiguous `ScriptAnalysisUnit` objects. The OpenAI-compatible model returns one classification object per unit ID; a pure validator/materializer combines those classifications with the trusted local ranges and text to produce the existing `ChunkAnalysisResult`, keeping all downstream APIs unchanged. Cache metadata is versioned so v1 index-based results are never reused by the v2 pipeline.

**Tech Stack:** Python 3.11, Pydantic v2, httpx/respx, FastAPI service modules, SQLAlchemy/SQLite, pytest/pytest-asyncio, uv, ruff, mypy.

## Global Constraints

- LLM analysis output must not contain `source_start`, `source_end`, or `source_text`.
- Local units must cover the complete chunk exactly once and preserve every Unicode character and whitespace character.
- Classification IDs must match local unit IDs exactly, in order, without duplicates or omissions.
- One invalid classification response receives one structured repair request; a second invalid response raises `LLM_INVALID_RESPONSE`.
- Existing public Director APIs, stored utterance records, role review, translation, generation, and WebUI behavior remain backward compatible.
- Old v1 analysis cache rows are ignored without a database migration or destructive cleanup.
- No fuzzy text matching or silent normalization is permitted.

---

### Task 1: Stable Local Units and Trusted Materialization

**Files:**
- Modify: `src/voice_pipeline/models/director_llm.py`
- Modify: `src/voice_pipeline/modules/llm/script_chunking.py`
- Modify: `tests/unit/test_director_llm_stages.py`

**Interfaces:**
- Produces: `ScriptAnalysisUnit(unit_id, source_start, source_end, source_text)`.
- Produces: `UnitAnalysis(unit_id, kind, temporary_role_name, role_aliases, role_confidence, speak_enabled)` and `UnitAnalysisResult(units)`.
- Produces: `build_analysis_units(chunk, max_unit_chars=160) -> tuple[ScriptAnalysisUnit, ...]`.
- Produces: `materialize_unit_analysis(chunk, units, result) -> ChunkAnalysisResult`.

- [ ] **Step 1: Write failing unit tests for lossless local splitting**

Add tests that pass Chinese dialogue, CRLF/newline whitespace, punctuation and a 200-character unpunctuated run to `build_analysis_units`. Assert:

```python
units = build_analysis_units(chunk)
assert "".join(unit.source_text for unit in units) == chunk.source_text
assert units[0].source_start == chunk.source_start
assert units[-1].source_end == chunk.source_end
assert all(left.source_end == right.source_start for left, right in pairwise(units))
assert all(len(unit.source_text) <= 160 for unit in units)
assert [unit.unit_id for unit in units] == [
    f"{chunk.chunk_id}:u{index:04d}" for index in range(len(units))
]
```

- [ ] **Step 2: Write failing materialization regression tests**

Construct `UnitAnalysisResult` with only stable IDs and classifications. Assert that materialized ranges and text come exclusively from the local units. Add parametrized missing, duplicate, reversed and unknown ID cases and assert `PipelineError.code == ErrorCode.LLM_INVALID_RESPONSE`.

The regression assertion must demonstrate that the classification objects have no index/text fields:

```python
annotation = UnitAnalysis(
    unit_id=units[0].unit_id,
    kind="narration",
    temporary_role_name=None,
    role_aliases=(),
    role_confidence=0.9,
    speak_enabled=True,
)
assert "source_start" not in annotation.model_fields
assert "source_text" not in annotation.model_fields
assert materialized.utterances[0].source_text == chunk.source_text[:expected_end]
```

- [ ] **Step 3: Run Task 1 tests and verify RED**

Run:

```powershell
uv run pytest tests/unit/test_director_llm_stages.py -q
```

Expected: collection/import failure because the new models and functions do not exist.

- [ ] **Step 4: Implement the minimal unit models and splitter**

Add strict Pydantic models to `director_llm.py`. In `script_chunking.py`, split after safe punctuation/newline or at 160 characters, carry leading blank spans into the next nonblank unit, append trailing blank spans to the last unit, use absolute Python indices, and derive IDs from `chunk.chunk_id` plus a zero-padded ordinal.

- [ ] **Step 5: Implement strict trusted materialization**

Validate the exact ordered ID tuple:

```python
expected_ids = tuple(unit.unit_id for unit in units)
actual_ids = tuple(item.unit_id for item in result.units)
if actual_ids != expected_ids:
    raise _invalid("analysis unit IDs must match the supplied units exactly and in order")
```

Create each `AnalyzedUtterance` with the local unit's range and text plus the LLM classification fields, then call `validate_chunk_analysis` before returning.

- [ ] **Step 6: Run Task 1 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/unit/test_director_llm_stages.py -q
uv run ruff check src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/script_chunking.py tests/unit/test_director_llm_stages.py
uv run mypy src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/script_chunking.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 1**

```powershell
git add src/voice_pipeline/models/director_llm.py src/voice_pipeline/modules/llm/script_chunking.py tests/unit/test_director_llm_stages.py
git commit -m "fix: materialize director analysis from local slices"
```

### Task 2: OpenAI Unit-ID Contract and One Repair Attempt

**Files:**
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `tests/unit/test_llm_client.py`

**Interfaces:**
- Consumes: `build_analysis_units` and `materialize_unit_analysis` from Task 1.
- Preserves: `OpenAiDirectorClient.analyze_script_chunk(...) -> ChunkAnalysisResult`.
- Sends: `{chunk_id, units: [{unit_id, source_text}]}` to the OpenAI-compatible endpoint.
- Receives: `UnitAnalysisResult` containing classification fields only.

- [ ] **Step 1: Write a failing request-contract test**

Use `respx` with a two-unit chunk and a response containing only `unit_id` plus classification fields. Call `analyze_script_chunk` and inspect the outgoing JSON:

```python
request_body = json.loads(route.calls[0].request.content)
system_prompt = request_body["messages"][0]["content"]
payload = json.loads(request_body["messages"][1]["content"])
assert set(payload["units"][0]) == {"unit_id", "source_text"}
assert "source_start" not in system_prompt
assert "source_end" not in system_prompt
assert result.utterances[0].source_text == original_source_slice
```

- [ ] **Step 2: Write a failing repair test**

Return a first HTTP 200 response with a missing/unknown unit ID and a second HTTP 200 response with all correct IDs. Assert two endpoint calls, a `retrying` activity event, and a valid materialized result. Add a second-invalid-response test that asserts `LLM_INVALID_RESPONSE` after exactly two application-level calls.

- [ ] **Step 3: Run Task 2 tests and verify RED**

Run:

```powershell
uv run pytest tests/unit/test_llm_client.py -q
```

Expected: new tests fail because the client still requests `ChunkAnalysisResult` with model-generated ranges/text.

- [ ] **Step 4: Replace the analysis prompt and output Schema**

Build units locally and send only IDs/text. Request `UnitAnalysisResult` and explicitly instruct the model to return exactly one classification for each supplied ID in the same order, without merging, omitting or inventing IDs. Do not send local indices.

- [ ] **Step 5: Add one structured repair attempt**

For materialization failures with `ErrorCode.LLM_INVALID_RESPONSE`, emit:

```text
分析单元 ID 校验失败，正在请求一次结构化修复
```

Append the invalid JSON as an assistant message and an instruction listing the exact expected IDs, then call `_post_json` once more with the same operation ID. Do not retry Pydantic schema failures that cannot provide a decoded object beyond the existing error path, and do not change HTTP retry counts.

- [ ] **Step 6: Run Task 2 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/unit/test_llm_client.py tests/unit/test_director_llm_stages.py -q
uv run ruff check src/voice_pipeline/modules/llm/client.py tests/unit/test_llm_client.py
uv run mypy src/voice_pipeline/modules/llm/client.py
```

Expected: all commands pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add src/voice_pipeline/modules/llm/client.py tests/unit/test_llm_client.py
git commit -m "fix: classify stable director analysis units"
```

### Task 3: Versioned Cache, Integration Regression, and Release Verification

**Files:**
- Modify: `src/voice_pipeline/core/director_analysis.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Modify: `tests/integration_cpu/test_director_analysis.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Changes: `DirectorStore.load_analysis_chunk(project_id, chunk, *, llm_fingerprint, prompt_version, schema_version)`.
- Changes: `DirectorStore.save_analysis_chunk(..., llm_fingerprint, prompt_version, schema_version)`.
- Uses cache constants `runtime-director-unit-ids-v2`, `director-analysis-units-v2`, and `2` from `director_analysis.py`.

- [ ] **Step 1: Write a failing cache-version integration test**

Save a valid chunk with v1 metadata, then analyze with a counting director configured for v2 and assert the LLM is called instead of loading the v1 result. Save/load v2 metadata and assert a second identical analysis reuses the v2 cache.

- [ ] **Step 2: Write a failing end-to-end mismatched-index regression test**

Add a staged director whose analysis behavior returns classification-only unit IDs and whose downstream cast/translation behavior comes from `FakeDirector`. Use a Chinese source longer than one unit and assert:

```python
project = await service.analyze(project.project_id, expected_revision=project.revision)
stored = await resources.list_utterances(project.project_id)
assert project.status == "role_review"
assert "".join(item.source_text for item in stored) == source
assert [(item.source_start, item.source_end) for item in stored] == expected_local_ranges
```

- [ ] **Step 3: Run Task 3 tests and verify RED**

Run:

```powershell
uv run pytest tests/integration_cpu/test_director_analysis.py -q
```

Expected: cache-version test fails because `load_analysis_chunk` ignores fingerprint/prompt/schema metadata.

- [ ] **Step 4: Enforce cache metadata on load and save**

Select and compare all three version fields before returning a cached result. Pass the v2 constants from `ScriptAnalysisService` to both load and save. Do not delete old rows; saving a v2 result updates the existing project/chunk row atomically and increments `attempt`.

- [ ] **Step 5: Update changelog**

Under `[Unreleased]`, document that Director Mode analysis now derives source ranges from deterministic local slice IDs instead of LLM-generated character offsets.

- [ ] **Step 6: Run focused integration verification**

Run:

```powershell
uv run pytest tests/unit/test_director_llm_stages.py tests/unit/test_llm_client.py tests/integration_cpu/test_director_analysis.py tests/integration_cpu/test_director_end_to_end.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Run complete static verification**

Run:

```powershell
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src workers
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/director.js
node --check src/voice_pipeline/webui/director-llm-activity.js
```

Expected: all checks pass.

- [ ] **Step 8: Run complete CPU/process suite without disturbing the live service**

The main checkout currently owns port 8765. Do not stop it without checking user jobs. Run all tests except the fixed-port lifecycle test first:

```powershell
uv run pytest -q -m "not gpu and not gpu_residency and not quality_model" --ignore=tests/process/test_start_stop_scripts.py
```

If the service is idle and can safely be stopped later, run the omitted process test separately; otherwise rely on unchanged process code plus GitHub CI, whose isolated runner executes the complete suite.

- [ ] **Step 9: Build and inspect the wheel**

```powershell
uv build --wheel --out-dir dist-director-stable-slices
```

Verify the wheel contains all modified Python modules and no local runtime/config/model artifacts.

- [ ] **Step 10: Commit Task 3**

```powershell
git add src/voice_pipeline/core/director_analysis.py src/voice_pipeline/storage/director_store.py tests/integration_cpu/test_director_analysis.py CHANGELOG.md
git commit -m "fix: invalidate legacy director analysis caches"
```

- [ ] **Step 11: Push, create PR, and wait for CI**

Push `codex/director-analysis-stable-slices`, open a PR against `main`, verify commit authors are `yjsnpi1145`, wait for Windows CI, and merge only after success.

## Plan Self-Review Result

- The plan covers every approved requirement: local stable IDs, lossless source coverage, classification-only output, one repair attempt, strict rejection, v1 cache invalidation, backward-compatible downstream objects, tests and release verification.
- No database migration or public API change is required.
- Type and function names are consistent across all tasks.
- No placeholder, fuzzy matching, unrelated retry tuning or UI scope remains.
