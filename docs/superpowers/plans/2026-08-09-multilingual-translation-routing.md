# Multilingual Translation Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every chapter segment carry target-language `synthesis_text` and independently validated Chinese `ref_text_cn`, so IndexTTS2 always speaks Chinese while GPT-SoVITS speaks the selected target language.

**Architecture:** Extend the existing single structured LLM director response instead of adding a second translation service or per-segment API calls. Preserve original source ranges for audit, persist the LLM-provided target translation in the existing `segments.synthesis_text` column, and enforce a shared Chinese-reference type before any GPU work.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, SQLAlchemy/SQLite, httpx OpenAI-compatible Chat Completions, pytest, vanilla HTML/JavaScript.

## Global Constraints

- `target_language` remains one of `zh`, `ja`, `en`, `ko`, `yue`.
- IndexTTS2 text and GPT-SoVITS `prompt_text` must always use validated Chinese `ref_text_cn`.
- GPT-SoVITS `text` must use `synthesis_text` in the selected `target_language`.
- The ordered source ranges must still cover the submitted source exactly, without gaps or overlaps.
- Translation occurs inside the one structured director call; no second translation provider or extra per-segment LLM loop is added.
- The generated GPT-SoVITS reference window remains the closed interval `3.0..10.0` seconds; uploaded IndexTTS2 base voice has no such duration window.
- Existing SQLite columns and migration revision remain unchanged.

---

### Task 1: Freeze bilingual segment schemas and materialization

**Files:**
- Modify: `src/voice_pipeline/models/schemas.py`
- Modify: `src/voice_pipeline/modules/llm/models.py`
- Modify: `src/voice_pipeline/modules/llm/director.py`
- Test: `tests/unit/test_schemas.py`
- Test: `tests/unit/test_llm_director.py`

**Interfaces:**
- Produces: `ChineseReferenceText`, an annotated string accepted only when nonblank, containing CJK and no Japanese kana.
- Produces: required `DirectedSegment.synthesis_text: NonBlankText`.
- Produces: `validate_director_plan()` preserving `segment.synthesis_text` while locally materializing `source_text` from the source range.

- [ ] **Step 1: Write failing schema and director tests**

```python
def test_directed_segment_rejects_non_chinese_reference_text() -> None:
    with pytest.raises(ValidationError):
        DirectedSegment(..., synthesis_text="これは本文です。", ref_text_cn="これは参考です。")


def test_materializer_preserves_target_language_translation() -> None:
    plan = DirectorPlan(..., segments=(DirectedSegment(
        ..., synthesis_text="これは翻訳です。", ref_text_cn="这是一句中文参考。"
    ),))
    assert validate_director_plan("这是原文。", plan)[0].synthesis_text == "これは翻訳です。"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_schemas.py tests/unit/test_llm_director.py`

Expected: failures because `DirectedSegment` has no required `synthesis_text`, Japanese reference text is accepted, or materialization overwrites the translation.

- [ ] **Step 3: Implement shared validation and schema**

```python
def _validate_chinese_reference_text(value: str) -> str:
    stripped = _validate_non_blank_text(value)
    if not any("\u3400" <= char <= "\u9fff" for char in stripped):
        raise ValueError("Chinese reference text must contain a CJK ideograph")
    if any("\u3040" <= char <= "\u30ff" for char in stripped):
        raise ValueError("Chinese reference text must not contain Japanese kana")
    return stripped


ChineseReferenceText = Annotated[str, AfterValidator(_validate_chinese_reference_text)]
```

Add `synthesis_text: NonBlankText` to `DirectedSegment`, use `ChineseReferenceText` for every public/persistent `ref_text_cn` entry point, and change materialization to:

```python
MaterializedDirectedSegment(**segment.model_dump(), source_text=source_slice)
```

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_schemas.py tests/unit/test_llm_director.py`

Expected: all selected tests pass.

### Task 2: Make the OpenAI-compatible director produce translation and Chinese reference text

**Files:**
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/modules/llm/fake.py`
- Test: `tests/unit/test_llm_client.py`
- Test: `tests/unit/test_llm_fake.py`

**Interfaces:**
- Consumes: required `DirectedSegment.synthesis_text` and `ChineseReferenceText` from Task 1.
- Produces: one `DirectorPlan` where `synthesis_text` is the full target-language rendering of its source range and `ref_text_cn` is Chinese.

- [ ] **Step 1: Update response fixtures first and assert prompt semantics**

The mocked completion segment must include:

```python
"synthesis_text": "これは目標言語の配音本文です。",
"ref_text_cn": "这是对应的中文情绪参考。",
```

Assert the system prompt contains all of:

```python
assert "synthesis_text must be written in target_language" in system_prompt
assert "translate the complete source slice" in system_prompt
assert "ref_text_cn must always be natural Simplified Chinese" in system_prompt
assert "never copy Japanese, English, Korean" in system_prompt
```

- [ ] **Step 2: Run client tests and confirm RED**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_llm_client.py tests/unit/test_llm_fake.py`

Expected: prompt assertions fail and fake plans fail the new required schema.

- [ ] **Step 3: Implement the structured translation prompt**

Require every segment to provide `synthesis_text`; instruct the model to copy the exact source slice only when its language already equals `target_language`, otherwise translate the complete slice without summary, explanation, omission or duplication. Require `ref_text_cn` to remain Simplified Chinese regardless of source and target language.

Update `FakeDirector` to set `synthesis_text=source_text[start:end]`; fake mode remains deterministic and does not pretend to provide production translation.

- [ ] **Step 4: Run client tests and confirm GREEN**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_llm_client.py tests/unit/test_llm_fake.py`

Expected: all selected tests pass.

### Task 3: Persist and execute the two text chains without regression

**Files:**
- Modify: `tests/unit/test_chapter_service_validation.py`
- Modify: `tests/integration_cpu/test_chapter_store.py`
- Modify: `tests/integration_cpu/test_chapter_pipeline.py`
- Modify any existing `DirectedSegment(...)` fixtures reported by strict schema failures.

**Interfaces:**
- Consumes: `ChapterStore.create_queued()` and existing `segments.synthesis_text/ref_text_cn` columns.
- Proves: Index jobs snapshot Chinese `ref_text_cn`; GSV jobs snapshot translated `synthesis_text` plus `target_language`; source text stays original.

- [ ] **Step 1: Add a Chinese-source/Japanese-target store test**

```python
assert stored.source_text == "这是原文。"
assert stored.synthesis_text == "これは翻訳です。"
assert stored.ref_text_cn == "这是中文情绪参考。"
assert stored.target_language == "ja"
```

- [ ] **Step 2: Add a chapter execution routing test**

Use a director returning Japanese `synthesis_text` and Chinese `ref_text_cn`; assert the Index job uses the Chinese text and the GSV job uses Japanese text with `text_lang="ja"`.

- [ ] **Step 3: Run chapter tests and confirm RED**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/unit/test_chapter_service_validation.py tests/integration_cpu/test_chapter_store.py tests/integration_cpu/test_chapter_pipeline.py`

Expected: old fixtures fail the required schema or the old materializer stores source text instead of translation.

- [ ] **Step 4: Update all affected fixtures, then confirm GREEN**

Add explicit `synthesis_text` values to every test/fake `DirectedSegment` constructor. Do not weaken strict schemas or add defaults.

Run the same command; expected: all selected tests pass.

### Task 4: Explain translation behavior in WebUI and public design

**Files:**
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `tests/contract/test_workbench_api.py`
- Modify: `Emotion_Driven_TTS_Pipeline_Design.md` (local design source, intentionally ignored by Git)

**Interfaces:**
- Produces: visible copy describing arbitrary-language input, automatic target translation, and always-Chinese IndexTTS2 reference text.

- [ ] **Step 1: Add failing static shell assertions**

```python
assert "原文可使用中文、日语、英语、韩语或其他语言" in page.text
assert "与原文不同时自动翻译" in page.text
assert "IndexTTS2 始终使用中文情绪参考文本" in page.text
```

- [ ] **Step 2: Run the contract test and confirm RED**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/contract/test_workbench_api.py -k static_shell`

- [ ] **Step 3: Add concise form help and bump static asset version**

Place the copy next to source text and target-language controls; do not add a new user decision or translation toggle.

- [ ] **Step 4: Run the contract test and confirm GREEN**

Run: `uv run --python .\.venv-control\Scripts\python.exe pytest -q tests/contract/test_workbench_api.py`

Expected: contract passes.

### Task 5: Full verification, commit and local deployment

**Files:**
- Verify all modified source, tests and documentation.

**Interfaces:**
- Produces: a clean Git commit and a restarted local service serving the new wheel.

- [ ] **Step 1: Run static verification**

```powershell
uv run --python .\.venv-control\Scripts\python.exe ruff check src workers tests
uv run --python .\.venv-control\Scripts\python.exe mypy
node --check src\voice_pipeline\webui\app.js
git diff --check
```

- [ ] **Step 2: Run the non-GPU suite**

```powershell
uv run --python .\.venv-control\Scripts\python.exe pytest -q -m "not gpu and not gpu_residency and not quality_model"
```

Expected: zero failures.

- [ ] **Step 3: Commit exact files**

```powershell
git add docs src tests
git commit -m "feat: route multilingual chapters through Chinese references"
```

- [ ] **Step 4: Build, install and restart**

Build one wheel into a commit-specific `runtime/control-wheel-*` directory, force-reinstall it into `.venv-control`, stop the current service through `/api/v1/control/shutdown`, start with `config/acceptance.gpu.local.yaml`, and wait for `/api/v1/health` to report `ready`.

- [ ] **Step 5: Verify served behavior**

Confirm the installed `DirectorPlan` schema requires `synthesis_text`, the served page contains the three translation messages, and health reports the configured real mode with an accepting GPU queue.
