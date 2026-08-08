# 批次 5：分块重生成、历史与显式重拼接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让本地工作台可安全地分别重生成 reference、GSV 或两者，浏览和激活不可变版本历史，并在用户明确要求时按当前选择重拼章节。

**Architecture:** 复用批次 2 的 `SegmentJobService`、持久 job store、版本 CAS 与单 GPU dispatcher；新增的项目层服务只负责冻结两阶段顺序、等待已提交 job 和将已有 `ChapterService` composition 显式公开。所有“是否过期”的徽标从当前 pointer 与不可变 version snapshot 推导，绝不写前端布尔值。

**Tech Stack:** FastAPI/Starlette、既有 SQLite/SQLAlchemy、Pydantic、原生 ES module、SoundFile/NumPy composer；不新增运行时依赖。

## Global Constraints

- 只运行于 loopback FastAPI；浏览器绝不能得到 API key、绝对模型路径、artifact 路径或 worker URL。
- 所有 TTS 通过既有 durable dispatcher 与单 consumer GPU queue；不直接调用模型 worker。
- `reference`、`gsv` 版本均不可变，普通历史/当前保护仍交给批次 2 retention；失败、取消或迟到结果不得破坏旧 pointer。
- 单独 GSV 重生成必须冻结并只使用提交时 `active_ref_version_id`；它不调用 IndexTTS2，也不读取未应用 `ref_draft`。
- 单独 reference 重生成绝不自动 GSV；`both` 仅在 reference 成功且被当前 CAS 激活后才提交 GSV。
- 历史试听只读；显式激活带 `selection_revision` CAS。重拼只读取 ordinal、当前 `active_gsv_version_id`、pause 和 output spec。
- 情绪向量固定八维、值域 0..1、总和 <= 0.8；编辑/恢复/归一化只改草稿，绝不自动推理。
- 开源复用清单必须记录所有通用库/前端方案；禁止复制上游模型代码。

---

## File Structure

| 文件 | 责任 |
| --- | --- |
| `src/voice_pipeline/core/regeneration_service.py` | 冻结单段 reference/GSV/both 命令、等待 durable job 与关闭时取消协调 task。 |
| `src/voice_pipeline/core/chapter_service.py` | 公开安全的 `recompose`，用唯一 staging 输出覆盖章节最终当前成品。 |
| `src/voice_pipeline/storage/chapter_store.py` | 将 explicit compose 原子发布到既有 chapter run、保留 timeline 快照。 |
| `src/voice_pipeline/api/workbench_routes.py` | regeneration、公共版本 history、activate、restore、compose 与状态 DTO。 |
| `src/voice_pipeline/api/app.py` | 构造/关闭 regeneration service。 |
| `src/voice_pipeline/webui/app.js` | 加入三个显式重生成按钮、版本 history/activate/restore、recompose 与 stale 徽标。 |
| `src/voice_pipeline/webui/index.html`, `styles.css` | 任务栏、history panel、状态和可访问按钮。 |
| `tests/integration_cpu/test_segment_regeneration.py` | reference/GSV/both、失败、迟到 CAS 及 recompose 链路。 |
| `tests/contract/test_regeneration_api.py` | 公共 HTTP/隐私/版本/compose 约定。 |
| `.acceptance/batch5_workbench/*` | ignored 的黑盒 HTTP/SQLite/browser-resource 验收。 |

### Task 1: 有界的单段重生成编排

**Files:** 创建 `src/voice_pipeline/core/regeneration_service.py`; 修改 `api/app.py`; 创建 `tests/integration_cpu/test_segment_regeneration.py`。

**Interfaces:**
```python
class SegmentRegenerationService:
    async def submit_reference(
        self, segment_id: UUID, request: SegmentReferenceJobRequest
    ) -> ExecutionContext: ...
    async def submit_gsv(
        self, segment_id: UUID, request: SegmentGsvJobRequest
    ) -> ExecutionContext: ...
    async def submit_both(
        self, segment_id: UUID, *, base_voice_path: Path, model_profile_id: UUID | None
    ) -> ExecutionContext: ...
    async def stop(self, *, deadline: float) -> None: ...
```

- [ ] **Step 1: Write failing integration tests.** Record initial ref/GSV pointers, submit GSV and assert its durable snapshot uses the old reference; mutate the ref draft and assert submit GSV still succeeds. Submit reference and assert dispatcher has no GSV job. Submit both and assert an Index job completes before one GSV job is enqueued.
- [ ] **Step 2: Run red tests.**
  Run: `uv run pytest tests/integration_cpu/test_segment_regeneration.py -q -W error`
  Expected: FAIL because `SegmentRegenerationService` does not exist.
- [ ] **Step 3: Implement only the service.** `submit_reference` and `submit_gsv` delegate once to `SegmentJobService` and notify dispatcher. `submit_both` delegates reference, starts a tracked coroutine that polls `SqliteJobStore.get`; it must submit GSV only after `status == succeeded` and the segment pointer equals the reference version committed by that job. A failed reference ends the coroutine without GSV; GSV failure leaves its newly active ref pointer intact. Use the same `PipelineError` mapping as `ChapterService._await_job`.
- [ ] **Step 4: Run green checks.**
  Run: `uv run pytest tests/integration_cpu/test_segment_regeneration.py -q -W error && uv run ruff check src/voice_pipeline/core/regeneration_service.py && uv run mypy src/voice_pipeline`
  Expected: PASS.
- [ ] **Step 5: Commit.**
  `git add src/voice_pipeline/core/regeneration_service.py src/voice_pipeline/api/app.py tests/integration_cpu/test_segment_regeneration.py && git commit -m "feat: orchestrate explicit segment regeneration"`

### Task 2: 版本状态、历史和显式重拼

**Files:** 修改 `models/chapter.py`, `storage/chapter_store.py`, `core/chapter_service.py`, `api/workbench_routes.py`; 创建 `tests/contract/test_regeneration_api.py`。

**Interfaces:**
```python
class SegmentWorkbenchState(StrictModel):
    segment_id: UUID
    reference_status: Literal["ready", "draft_pending", "missing"]
    gsv_status: Literal["ready", "stale", "missing"]
    active_ref_version_id: UUID | None
    active_gsv_version_id: UUID | None


async def recompose(self, run_id: UUID) -> ChapterRunRecord: ...


POST / api / v1 / segments / {segment_id} / history
POST / api / v1 / segments / {segment_id} / versions / {version_id} / activate
POST / api / v1 / chapters / {run_id} / compose
```

- [ ] **Step 1: Write failing contract tests.** Confirm histories are path-free and grouped by `reference`/`gsv`; activating a historical version requires current selection revision; status becomes stale if active GSV’s `ref_version_id` differs from active ref; compose refuses a run with a missing current GSV and succeeds with the exact selected IDs.
- [ ] **Step 2: Run red tests.**
  Run: `uv run pytest tests/contract/test_regeneration_api.py -q -W error`
  Expected: FAIL with missing route/service.
- [ ] **Step 3: Implement public DTOs and compose.** Build stale state by comparing current segment input fields with version `input_snapshot` and comparing `gsv.ref_version_id` with `active_ref_version_id`. Public history must omit `blob_relative_path`, manifest filesystem paths and model paths but retain version ID, type, source job ID, frozen input snapshot, reference binding ID, quality summary and `/audio` URL. `recompose` must stage a new WAV/timeline then atomically replace a chapter-run final record only after all current GSV pointers are ready; on failure retain prior final audio/timeline.
- [ ] **Step 4: Run green checks.**
  Run: `uv run pytest tests/contract/test_regeneration_api.py tests/unit/test_audio_composer.py -q -W error && uv run ruff check src/voice_pipeline && uv run mypy src/voice_pipeline`
  Expected: PASS.
- [ ] **Step 5: Commit.**
  `git add src/voice_pipeline/models src/voice_pipeline/storage/chapter_store.py src/voice_pipeline/core/chapter_service.py src/voice_pipeline/api/workbench_routes.py tests/contract/test_regeneration_api.py && git commit -m "feat: expose version history and explicit chapter compose"`

### Task 3: Workbench command and history UX

**Files:** 修改 `webui/index.html`, `webui/app.js`, `webui/styles.css`; 修改 `tests/contract/test_workbench_api.py`, `tests/integration_cpu/test_webui_workbench.py`。

- [ ] **Step 1: Write failing static/integration tests.** Require the page JS to reference only `/api/v1/segments/.../regenerate-*`, history/activate and chapter compose endpoints; assert it has no worker URL or API-key string. Verify a fake completed chapter can click-equivalent submit GSV without changing reference hash/ID, activate history via CAS, and compose only on explicit command.
- [ ] **Step 2: Run red tests.**
  Run: `uv run pytest tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py -q -W error`
  Expected: FAIL because the command/history controls are absent.
- [ ] **Step 3: Implement minimal accessible UI.** Add buttons labelled “重新生成参考音频”, “重新生成 GSV”, “重新生成两者”, “重新拼接整篇”; disable only reference/both when vector total exceeds .8. Show submitted job ID/status and the frozen reference ID near GSV. Fetch public history, render audio controls and activate buttons with latest `selection_revision`; history playback does not write. Re-fetch state after every command/SSE event. Do not implement implicit model calls.
- [ ] **Step 4: Run green checks.**
  Run: `uv run pytest tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py -q -W error && uv run ruff format --check . && uv run ruff check .`
  Expected: PASS.
- [ ] **Step 5: Commit.**
  `git add src/voice_pipeline/webui tests/contract/test_workbench_api.py tests/integration_cpu/test_webui_workbench.py && git commit -m "feat: add regeneration and version history workbench controls"`

### Task 4: Documentation and independent acceptance

**Files:** 修改 `README.md`, `config/open-source-reuse.yaml`; 创建 `.acceptance/batch5_workbench/run_acceptance.py`, `.acceptance/batch5_workbench/test_harness_self.py`; 写入 ignored `runtime/handoff/batch5-*.json`。

- [ ] **Step 1: Write failing black-box assertions.** The harness must launch only a fake loopback control plane, create a chapter, prove GSV-only preserves reference SHA/version, prove reference-only enqueues no GSV, prove both retains new ref after injected GSV failure, activate historical version with CAS, explicitly compose, and verify SQLite `integrity_check`/`foreign_key_check`.
- [ ] **Step 2: Run self-test before implementation.**
  Run: `uv run pytest .acceptance/batch5_workbench/test_harness_self.py -q -W error`
  Expected: PASS; mutant private payload and wrong pointer cases rejected.
- [ ] **Step 3: Document usage and reuse boundary.** Document local-only endpoints/UI command meanings, explicitly state no automatic generation/recomposition, and record reused FastAPI/Starlette/native browser/SoundFile components plus rejected frontend frameworks.
- [ ] **Step 4: Run full validation and independent harness.**
  Run: `uv sync --frozen --extra dev --python 3.11; uv lock --check; uv run python -m compileall -q src workers; uv run ruff format --check .; uv run ruff check .; uv run mypy src/voice_pipeline workers; uv run pytest tests -m 'not gpu and not gpu_residency and not quality_model' -vv -W error --cov=voice_pipeline --cov=workers.indextts2 --cov-branch --cov-fail-under=85; uv run python .acceptance/batch5_workbench/run_acceptance.py`
  Expected: source checks and fake black-box PASS. Real model/browser listening remains a separately labelled BLOCKED prerequisite if assets are absent.
- [ ] **Step 5: Commit tracked documents.**
  `git add README.md config/open-source-reuse.yaml docs/superpowers/plans/2026-08-08-batch-5-regeneration-history-and-recompose.md && git commit -m "docs: document segment regeneration workbench"`

## Final self-review checklist

- [ ] Reference-only never calls GSV; GSV-only preserves ref SHA/version even with a dirty ref draft; both has the specified two-stage failure behavior.
- [ ] Late results become immutable history and cannot override later draft/selection changes.
- [ ] UI status derives from pointers/snapshots, history audio cannot select implicitly, and recompose is explicit.
- [ ] All public DTOs/resources are local, path-free, secret-free and wheel packaged; real quality/listening results are never represented by fake success.
