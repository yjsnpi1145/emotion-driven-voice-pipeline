# Director Reference Expansion and Sentence Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Director-mode short dialogue generate valid 3–10 second Chinese references without changing reviewed dialogue/emotion, support explicitly skipped roles, and export successful sentence WAVs as an ordered ZIP alongside the mixed WAV.

**Architecture:** Reuse the existing `ReferenceTextDirector` audio-feedback loop inside `DirectorGenerationService`, persist only corrected `ref_text_cn`, represent skip intent with a durable role boolean, and build a deterministic ZIP from ready GSV artifact blobs during composition. Existing APIs remain compatible while the WebUI gains an explicit skip mapping and ZIP download.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLAlchemy/Alembic/SQLite, asyncio, stdlib `zipfile`, vanilla JavaScript, pytest.

## Global Constraints

- Never modify `source_text`, `synthesis_text`, emotion vector, role, speed, pause, or seed during reference correction.
- IndexTTS reference duration is the closed interval `3.0..10.0` seconds with at most two LLM corrections.
- Unconfigured roles block generation; explicitly skipped roles create no audio work or export entry.
- Preserve the mixed WAV export and add an ordered sentence ZIP.
- ZIP names are `NNNN_<sanitized Chinese source sentence>.wav`.

---

### Task 1: Persist explicit role skip mappings

**Files:**
- Create: `src/voice_pipeline/storage/migrations/versions/0007_director_role_dubbing.py`
- Modify: `src/voice_pipeline/storage/orm.py`
- Modify: `src/voice_pipeline/storage/database.py`
- Modify: `src/voice_pipeline/models/director.py`
- Modify: `src/voice_pipeline/storage/director_store.py`
- Modify: `src/voice_pipeline/api/director_routes.py`
- Test: `tests/integration_cpu/test_director_migration.py`
- Test: `tests/integration_cpu/test_director_store.py`
- Test: `tests/contract/test_director_api.py`

**Interfaces:**
- Produces: `DirectorRoleRecord.dubbing_enabled: bool`.
- Produces: `BindRolePresetRequest.mapping_mode: Literal["preset", "skip"]` and `preset_id: UUID | None`.
- Produces: `DirectorStore.bind_role_preset(..., preset_id: UUID | None, dubbing_enabled: bool)`.

- [ ] **Step 1: Write failing migration/model/store/API tests**

```python
assert migrated_role["dubbing_enabled"] is True
skip = BindRolePresetRequest(expected_revision=0, mapping_mode="skip", preset_id=None)
assert skip.mapping_mode == "skip"
with pytest.raises(ValidationError):
    BindRolePresetRequest(expected_revision=0, mapping_mode="preset", preset_id=None)
record = await store.bind_role_preset(
    role_id, expected_revision=0, preset_id=None, dubbing_enabled=False
)
assert record.dubbing_enabled is False and record.preset_id is None
```

- [ ] **Step 2: Run the focused tests and confirm the missing column/contracts fail**

Run: `uv run pytest tests/integration_cpu/test_director_migration.py tests/integration_cpu/test_director_store.py tests/contract/test_director_api.py -q`

- [ ] **Step 3: Add migration and mapping invariants**

```python
revision = "0007_director_role_dubbing"
down_revision = "0006_director_preprocessing"

op.add_column(
    "director_roles",
    sa.Column("dubbing_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
)
```

```python
class BindRolePresetRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    mapping_mode: Literal["preset", "skip"] = "preset"
    preset_id: UUID | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> BindRolePresetRequest:
        if (self.mapping_mode == "preset") != (self.preset_id is not None):
            raise ValueError("preset mapping requires preset_id and skip mapping forbids it")
        return self
```

- [ ] **Step 4: Run focused tests**

Expected: migration, store, and contract tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/voice_pipeline/storage src/voice_pipeline/models/director.py src/voice_pipeline/api/director_routes.py tests
git commit -m "feat: add explicit director role skip mapping"
```

### Task 2: Expand and duration-correct Director reference text

**Files:**
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/api/app.py`
- Test: `tests/unit/test_llm_client.py`
- Test: `tests/integration_cpu/test_director_end_to_end.py`

**Interfaces:**
- Consumes: runtime LLM `correct_reference_text`, `SynthesisService`, `SerialGpuQueue`, and `ReferenceTextDirector`.
- Produces: `DirectorGenerationService._resolve_reference_text(segment, utterance, base_voice) -> SegmentRecord`.

- [ ] **Step 1: Write failing tests for immutable fields and duration feedback**

```python
resolved = await service._resolve_reference_text(segment, utterance, base_voice)
assert resolved.ref_text_cn == "LLM 扩展后且足够自然的中文情绪参考台词。"
assert resolved.source_text == segment.source_text
assert resolved.synthesis_text == segment.synthesis_text
assert resolved.current_emotion_vector == segment.current_emotion_vector
assert resolved.speed_factor == segment.speed_factor
assert resolved.pause_after_ms == segment.pause_after_ms
assert probe.durations == []
```

- [ ] **Step 2: Run focused tests and confirm Director generation currently submits the short reference directly**

Run: `uv run pytest tests/unit/test_llm_client.py tests/integration_cpu/test_director_end_to_end.py -q`

- [ ] **Step 3: Strengthen initial LLM instructions and reuse correction loop**

```python
"For short dialogue, expand ref_text_cn into a natural Simplified-Chinese performance "
"reference expected to speak for 3 to 10 seconds. Preserve the reviewed emotion; do not "
"change synthesis_text, source boundaries, role, speed, pause, seed, or emotion_vector."
```

Before `submit_reference`, resolve and persist the reference text through `ReferenceTextDirector`; then submit the durable job against the patched segment.

- [ ] **Step 4: Filter skipped roles before snapshot/materialization**

```python
mapped_roles = {rid: role for rid, role in roles.items() if role.dubbing_enabled}
utterances = [item for item in utterances if item.role_id in mapped_roles]
```

Unconfigured mapped roles still raise `ROLE_PRESET_UNAVAILABLE`; an empty mapped utterance list raises `DIRECTOR_REVIEW_REQUIRED`.

- [ ] **Step 5: Run focused tests**

Expected: short references are corrected, immutable inputs are unchanged, and skipped roles create no jobs.

- [ ] **Step 6: Commit**

```powershell
git add src/voice_pipeline/modules/llm/client.py src/voice_pipeline/core/director_generation.py src/voice_pipeline/api/app.py tests
git commit -m "feat: expand short director reference text"
```

### Task 3: Build and serve sentence ZIP exports

**Files:**
- Create: `src/voice_pipeline/core/sentence_archive.py`
- Modify: `src/voice_pipeline/core/director_generation.py`
- Modify: `src/voice_pipeline/api/director_routes.py`
- Test: `tests/unit/test_sentence_archive.py`
- Test: `tests/contract/test_director_api.py`
- Test: `tests/integration_cpu/test_director_end_to_end.py`

**Interfaces:**
- Produces: `sanitize_sentence_filename(ordinal: int, source_text: str, *, max_stem_chars: int = 80) -> str`.
- Produces: `write_sentence_archive(entries: Sequence[SentenceArchiveEntry], output_path: Path) -> Path`.
- Produces: `GET /api/v1/director-projects/{project_id}/sentence-audio.zip`.

- [ ] **Step 1: Write failing filename and ZIP tests**

```python
assert sanitize_sentence_filename(0, '“为什么:这样/呢？”') == "0001_“为什么这样呢？”.wav"
assert sanitize_sentence_filename(1, "  \n ") == "0002_句子.wav"
with ZipFile(archive) as bundle:
    assert bundle.namelist() == ["0001_第一句.wav", "0002_第一句.wav"]
```

- [ ] **Step 2: Run focused tests and confirm missing archive implementation**

Run: `uv run pytest tests/unit/test_sentence_archive.py tests/contract/test_director_api.py -q`

- [ ] **Step 3: Implement deterministic safe ZIP writing**

```python
_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize_sentence_filename(ordinal: int, source_text: str, *, max_stem_chars: int = 80) -> str:
    stem = _WINDOWS_INVALID.sub("", " ".join(source_text.split())).strip(" .")[:max_stem_chars]
    return f"{ordinal + 1:04d}_{stem or '句子'}.wav"
```

Write to a temporary archive and atomically replace the target. Add each ready GSV blob with `ZIP_DEFLATED` in utterance order.

- [ ] **Step 4: Generate archive next to each mixed WAV and add guarded download route**

The route derives `sentences.zip` from `generation.final_relative_path`, verifies containment under `artifacts/directors`, rejects symlinks/missing files, and responds with `application/zip`.

- [ ] **Step 5: Run focused tests**

Expected: deterministic ordered ZIP, Chinese names, route 200 when ready and 409 otherwise.

- [ ] **Step 6: Commit**

```powershell
git add src/voice_pipeline/core src/voice_pipeline/api/director_routes.py tests
git commit -m "feat: export director audio by sentence"
```

### Task 4: Product UI, regression verification, and deployment

**Files:**
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `src/voice_pipeline/webui/index.html`
- Test: `tests/integration_cpu/test_webui_workbench.py`
- Test: `tests/unit/test_director_models.py`

**Interfaces:**
- Consumes: role `dubbing_enabled`, mapping modes, and sentence ZIP endpoint.
- Produces: visible “不予映射（跳过配音）” choice and “下载逐句 ZIP” control.

- [ ] **Step 1: Write failing WebUI assertions**

```python
assert "不予映射（跳过配音）" in director_js
assert "mapping_mode" in director_js
assert "sentence-audio.zip" in director_js
assert "下载逐句 ZIP" in director_js
```

- [ ] **Step 2: Implement mapping and export controls**

Use a sentinel select value `__skip__`; submit `{mapping_mode: "skip", preset_id: null}` for it and `{mapping_mode: "preset", preset_id}` for a real preset. Keep the empty placeholder distinct. Render both mixed WAV and ZIP download links only for successful generations.

- [ ] **Step 3: Run the complete CI-equivalent suite**

```powershell
uv run ruff check src tests
uv run mypy src workers
node --check src/voice_pipeline/webui/app.js
node --check src/voice_pipeline/webui/director.js
uv run pytest -q -m "not gpu and not gpu_residency and not quality_model"
uv build --wheel --out-dir dist
```

- [ ] **Step 4: Install, migrate, restart, and smoke test**

Stop through `scripts/stop.ps1`, install the wheel into `.venv-control`, run `启动服务.bat`, assert health/storage/quality `ready`, dispatcher `running`, WebUI HTTP 200, Alembic revision `0007_director_role_dubbing`, skip mapping persistence, and ZIP endpoint behavior.

- [ ] **Step 5: Review, push, wait for CI, and merge**

Commit with Git identity `yjsnpi1145`, push `codex/director-reference-expansion-export`, create a PR, resolve all Critical/Important review findings, require green CI, and merge into `main`.
