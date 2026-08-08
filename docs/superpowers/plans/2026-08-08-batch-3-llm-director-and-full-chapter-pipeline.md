# 批次 3：LLM Director 与整篇自动配音 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过 OpenAI 兼容 LLM API，将章节原文划分为可追溯分块，生成全部参考/GSV 音频，并输出含停顿的 `final.wav` 与时间线 Manifest。

**Architecture:** 使用 HTTPX 实现很薄的 OpenAI `/chat/completions` client，并在本地严格验证 JSON、全文 Unicode 字符区间和情绪向量。章节服务把计划冻结为批次 2 的 task/segment/versions，按 ordinal 串行提交既有 durable reference/GSV jobs；Composer 只拼接当前 ready GSV 版本。章节运行、输入 snapshot 和输出也持久化；本批次不实现 WebUI 或局部重生成用户流程。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、HTTPX、SQLite/Alembic、soundfile、numpy、Typer。

## Global Constraints

- 仅 loopback 控制面；CLI 只通过 HTTP，禁止直接读服务器 runtime 或导入引擎。
- Python `>=3.11,<3.12`；不得加入 Redis、Celery、分布式队列或多 GPU。
- 所有推理由既有 `SerialGpuQueue` 串行执行，最大活跃 GPU 推理数为 1。
- API key 仅从 `llm.api_key_env` 环境变量读取，绝不进入 DB、manifest、日志、错误详情或 CLI JSON。
- `source_start`/`source_end` 为 Python Unicode 字符索引 `[start,end)`，必须从 0 无缺口、无重叠覆盖 `len(source_text)`；程序用原文切片生成 `source_text`/`synthesis_text`，永不采用 LLM 给出的正文。
- 向量顺序固定 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`，8 项、每项 `0..1`、合计 `<=0.8`；只做稳定序列化，绝不隐藏归一化或 emotion bias 乘法。
- 参考文字时长为 3–9 秒；最多 `llm.max_reference_corrections` 次。校正响应只含 `ref_text_cn`，不能改变区间、向量、速度、停顿、语言。
- 快照必须含 LLM model、原文 SHA-256、完整 director 计划、最终 ref text、base voice SHA、GSV profile snapshot、输出规格、seed、engine fingerprints。
- `final.wav` 只能由各 ordinal 的明确当前、ready GSV 版本合成；最后段无尾部停顿；缺任一段则失败且不创建 partial final。
- 继续维护 `docs/open-source-reuse.yaml`：HTTPX（BSD-3-Clause）、Pydantic（MIT）、NumPy（BSD-3-Clause）、SoundFile（BSD-3-Clause）均直接复用；拒绝 `openai-python`，理由为 HTTPX 已覆盖兼容协议且可减小依赖面。
- 不做批次 4 WebUI/SSE，也不做批次 5 草稿/激活/局部重生成交互。

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `modules/llm/models.py` | LLM wire schema、已验证 Director 计划与校正响应。 |
| `modules/llm/client.py` | JSON-mode OpenAI-compatible HTTPX client，有限重试与密钥保护。 |
| `modules/llm/director.py` | 区间/哈希/向量验证、原文切片、参考文本校正循环。 |
| `modules/llm/fake.py` | fake/external 测试的可重复分块 director。 |
| `models/chapter.py` | 章节请求、状态、timeline 与运行记录 schema。 |
| `storage/chapter_store.py` | 章节运行及冻结快照的 SQLite repository。 |
| `storage/orm.py`、`migrations/versions/0002_batch3_chapter_runs.py` | `chapter_runs`/`chapter_run_segments` 与升级。 |
| `modules/audio/composer.py` | 读取 ready GSV WAV、插入停顿、原子发布 WAV/timeline。 |
| `core/chapter_service.py` | 物化分块，串行调度 reference/GSV，并合成成品。 |
| `api/chapter_routes.py`、`api/app.py`、`cli.py` | `/api/v1/chapters` 和 HTTP-only CLI。 |
| `tests/unit/test_llm_*.py`、`tests/unit/test_audio_composer.py` | 无引擎的 client/director/composer 测试。 |
| `tests/integration_cpu/test_chapter_*.py`、`tests/contract/test_chapter_api_cli.py` | fake LLM/engine 端到端与对外契约。 |

### Task 1: 冻结配置、Schema 和迁移

**Files:** 修改 `src/voice_pipeline/core/config.py`, `src/voice_pipeline/storage/orm.py`, `src/voice_pipeline/storage/database.py`, `config/app.fake.yaml`, `config/app.real.example.yaml`；创建 `src/voice_pipeline/models/chapter.py`, `src/voice_pipeline/storage/migrations/versions/0002_batch3_chapter_runs.py`；测试 `tests/unit/test_config.py`, `tests/unit/test_chapter_models.py`, `tests/integration_cpu/test_database_migrations.py`。

**Produces:**
```python
class LlmSettings(BaseModel):
    mode: Literal["fake", "openai"] = "fake"
    base_url: str
    model: NonBlankText
    api_key_env: str | None
    timeout_seconds: float = Field(gt=0, le=300)
    max_retries: int = Field(ge=0, le=5)
    max_reference_corrections: int = Field(ge=0, le=5)


class ChapterSynthesisRequest(StrictModel):
    request_id: UUID
    title: NonBlankText
    source_text: NonBlankText
    target_language: LanguageCode
    base_voice_path: Path
    model_profile_id: UUID
    output_spec: OutputAudioSpec
    seed: int
```
`ChapterRunRecord` 必须有 `run_id/task_id/status/snapshot/director_plan/error/final_audio/timeline/created_at/started_at/finished_at`。迁移从 `0001_batch2_foundation` 创建 chapter tables，`chapter_run_segments` 用 `(run_id, ordinal)` 主键和 segment FK；`PACKAGED_HEAD` 变为 `0002_batch3_chapter_runs`。

- [ ] 写失败测试：openai mode 缺 `api_key_env` 应 `ValidationError`；空章节/无 profile 应失败；migration 后版本为 `0002_batch3_chapter_runs`。
- [ ] 运行 `uv run pytest tests/unit/test_config.py tests/unit/test_chapter_models.py tests/integration_cpu/test_database_migrations.py -q`，确认红。
- [ ] 实现上述 schema/migration；`AppSettings` 加 `llm: LlmSettings`，fake 配置填 `mode: fake`，real example 用 `mode: openai` + 环境变量名。
- [ ] 运行同一 pytest、`uv run ruff check src/voice_pipeline tests`、`uv run mypy src/voice_pipeline`，确认绿。
- [ ] 提交：`git commit -m "feat: define durable chapter run contracts"`。

### Task 2: OpenAI-compatible client 与 source-safe director

**Files:** 创建 `src/voice_pipeline/modules/llm/__init__.py`, `models.py`, `client.py`, `director.py`；修改 `core/errors.py`；测试 `tests/unit/test_llm_client.py`, `tests/unit/test_llm_director.py`。

**Produces:**
```python
class DirectedSegment(StrictModel):
    ordinal: int
    source_start: int
    source_end: int
    emotion_description: NonBlankText
    emotion_vector: EmotionVector
    ref_text_cn: NonBlankText
    pause_after_ms: int
    speed_factor: float
    seed: int


class DirectorPlan(StrictModel):
    source_text_sha256: str
    segments: tuple[DirectedSegment, ...]


async def create_plan(source_text: str, target_language: LanguageCode) -> DirectorPlan: ...
async def correct_reference_text(
    current: str, direction: Literal["shorten", "lengthen"], emotion_description: str
) -> str: ...
def validate_director_plan(
    source_text: str, response: DirectorPlan
) -> tuple[MaterializedDirectedSegment, ...]: ...
```

- [ ] 用 `respx` 写失败测试：请求是 `POST /chat/completions`、header 有 Bearer 但 plan 的 repr/错误中没有 secret；JSON/sha/gap/overlap/越界/向量非法在任一引擎调用前被拒绝；切片保持中文/日文 Unicode 原文。
- [ ] 运行 `uv run pytest tests/unit/test_llm_client.py tests/unit/test_llm_director.py -q`，确认红。
- [ ] 实现 client：`temperature: 0`、`response_format: {"type":"json_object"}`；只重试网络异常和 `429/502/503/504`，次数 `max_retries+1`，延时 0.25/0.5/1.0 秒。只在去掉 fence 后为合法 JSON 时解析。添加不含 headers/body 的 `LLM_UNAVAILABLE`、`LLM_INVALID_RESPONSE` errors。
- [ ] `validate_director_plan` 验 SHA、`start==previous_end`、`first=0`、`last=len(text)`、速度 0.5–2、pause 0–30000；用 `source_text[start:end]` 同时写 source/synthesis。
- [ ] 运行 focused pytest + `ruff` + `mypy`，提交 `feat: add source-safe OpenAI compatible director`。

### Task 3: 参考文本长度修正与 fake Director

**Files:** 修改 `modules/llm/director.py`；创建 `modules/llm/fake.py`；测试 `tests/unit/test_llm_director.py`, `tests/unit/test_llm_fake.py`。

**Consumes/Produces:**
```python
class ReferenceDurationProbe(Protocol):
    async def generate_and_measure(self, text: str, vector: EmotionVector, seed: int) -> float: ...


async def resolve_reference_text(
    segment: DirectedSegment, probe: ReferenceDurationProbe
) -> ResolvedDirectedSegment: ...
```

- [ ] 写失败测试，probe 依次返回 `2.2, 10.1, 4.0` 时只变 ref text、保留向量/区间并记录 correction=2；超过次数仍未进 3–9 时抛 `REFERENCE_DURATION_INVALID`。
- [ ] 运行 `uv run pytest tests/unit/test_llm_director.py tests/unit/test_llm_fake.py -q`，确认红。
- [ ] 实现循环：每次测量；短则调用 `direction="lengthen"`，长则 `"shorten"`；最后一次不合格即错误。校正 JSON 模型为 `extra="forbid"`，只允许 `ref_text_cn`。
- [ ] fake Director 按标点（否则 40 字）分割，必覆盖全文，产出合法 8D 向量和固定中文参考句，校正保持同一句以适配 fake Index 4 秒输出。
- [ ] 运行测试并提交 `feat: correct reference text durations through director`。

### Task 4: 章节持久化和分块物化

**Files:** 创建 `storage/chapter_store.py`；修改 `storage/segment_store.py`, `models/chapter.py`；测试 `tests/integration_cpu/test_chapter_store.py`。

**Produces:**
```python
async def create_queued(
    request: ChapterSynthesisRequest,
    plan: DirectorPlan,
    profile: GsvModelSnapshot,
    base_voice_sha256: str,
) -> ChapterRunRecord: ...
async def mark_running(run_id: UUID) -> ChapterRunRecord: ...
async def mark_succeeded(
    run_id: UUID, final: AudioResult, timeline: ChapterTimeline
) -> ChapterRunRecord: ...
async def mark_failed(run_id: UUID, error: dict[str, JsonValue]) -> ChapterRunRecord: ...
async def mark_interrupted_running() -> tuple[UUID, ...]: ...
async def create_segments(
    task_id: UUID, requests: tuple[CreateSegmentRequest, ...]
) -> tuple[SegmentRecord, ...]: ...
```

- [ ] 写失败集成测试：一次 `create_queued` 在同一事务创建 task/segments/run 映射；连续 source text 正确；`running -> interrupted` 不删除 segments。
- [ ] 运行红测。
- [ ] 用一个 `Database.write_session` 插入 task、所有 segments、run 和 mappings，先 canonical JSON snapshot 后才可排队；ordinal 唯一；失败 rollback。重启只把 running 标 interrupted，永不自动重跑。
- [ ] 回归 `test_segment_versions`、migration；提交 `feat: persist chapter runs and directed segments`。

### Task 5: 选中版本的 WAV composer 和 timeline

**Files:** 创建 `modules/audio/composer.py`；测试 `tests/unit/test_audio_composer.py`。

**Produces:**
```python
class ComposeInput(StrictModel):
    ordinal: int
    segment_id: UUID
    gsv_version: ArtifactVersionView
    blob_path: Path
    pause_after_ms: int


def compose_final(
    *,
    ordered_inputs: tuple[ComposeInput, ...],
    output_spec: OutputAudioSpec,
    output_path: Path,
    timeline_path: Path,
) -> ComposedChapterAudio: ...
```

- [ ] 写失败测试：1 秒+500ms+2秒 的时长约 3.5 秒；timeline 第二段从 1.5 开始；最后段的 999ms pause 不生效；缺/非 ready 文件失败且没有 final。
- [ ] 运行红测。
- [ ] 在 reserve 任一输出之前验证所有 inputs/state/path；`soundfile.read(..., always_2d=True)`、numpy mean 单声道、`numpy.interp` 只在需重采样时使用、`numpy.zeros(round(sr*pause/1000))` 静音；PCM16 temp WAV + `probe_wav` + `reserve_output_path` 原子发布，timeline 用 `atomic_write_json`。
- [ ] 运行 focused tests/lint/type check；提交 `feat: compose selected segment audio into final wav`。

### Task 6: 串行章节 orchestration 与 lifecycle

**Files:** 创建 `core/chapter_service.py`；修改 `api/app.py`, `api/dependencies.py`, `core/errors.py`；测试 `tests/integration_cpu/test_chapter_pipeline.py`, `tests/unit/test_app_injection_branches.py`。

**Produces:**
```python
async def submit(request: ChapterSynthesisRequest) -> ChapterRunRecord: ...
async def get(run_id: UUID) -> ChapterRunRecord: ...
async def recover() -> tuple[UUID, ...]: ...
async def stop(*, deadline: float) -> None: ...
```

- [ ] 写 fake 端到端红测：提交多段章节、最后 succeeded、有每段 reference/GSV version、下载 final 有效；engine failure 后 run failed、旧版本在、final 不存在。
- [ ] 运行红测。
- [ ] `submit` 先 resolve/freeze GSV profile/base voice SHA/director plan，再 Task4 物化，最后 `asyncio.create_task(_run(run_id))`。`_run` 对每 ordinal：先 `SegmentJobService.submit_reference` 并轮询 `SqliteJobStore` 至 terminal；再 submit GSV 使用 frozen profile；每个 engine job 新 request UUID；绝不并行。duration probe 使用 disposable non-versioned Index context，校正后用 revision CAS 更新 segment 再提交唯一 versioned reference。随后读取每段当前 ready GSV 交 Task5 composer，结果固定在 `artifact_root/chapters/<run_id>/final.wav`/`timeline.json`。
- [ ] app startup 先 `recover`；shutdown 在共享 deadline cancel chapter tasks，绝不删除完成 artifact。运行 durable job regressions 后提交 `feat: orchestrate durable full chapter synthesis`。

### Task 7: REST/CLI 和复用清单

**Files:** 创建 `api/chapter_routes.py`；修改 `api/app.py`, `cli.py`, `docs/open-source-reuse.yaml`；测试 `tests/contract/test_chapter_api_cli.py`, `tests/contract/test_open_source_reuse.py`。

**Public contract:**
```
POST /api/v1/chapters                 -> 202 {run_id,request_id,status:"queued"}
GET  /api/v1/chapters/{run_id}        -> ChapterRunRecord
GET  /api/v1/chapters/{run_id}/audio  -> final.wav only after succeeded
GET  /api/v1/chapters/{run_id}/timeline -> JSON only after succeeded
voice-pipeline synthesize-chapter --server URL --request chapter.json --output-dir DIR --timeout-seconds N --json
```

- [ ] 写失败 contract 测试：CLI 只使用 HTTP、下载 `final.wav`/`timeline.json`，已有 output collision 不覆盖；状态/错误不泄露 API key/base voice absolute path。
- [ ] 运行红测。
- [ ] 复用 `foundation_routes` error envelope/UUID 解析和 CLI `_poll_job`/reservation helpers；generalize polling to all chapter terminal states。路由验证输出一定在 artifact root。写完整 reuse 条目，含 SPDX、range、source、采用边界、拒绝 `openai-python` 理由。
- [ ] 运行 contract/type/lint 后提交 `feat: expose chapter synthesis API and cli`。

### Task 8: 独立验收和交付证据

**Files:** 创建 gitignored `.acceptance/batch3_blackbox/{fake_openai_server.py,run_acceptance.py,test_harness_self.py}`、`runtime/handoff/{batch3-developer-report.json,batch3-acceptance.json}`；修改 `README.md`, `docs/open-source-reuse.yaml`。

- [ ] 先写 harness mutant tests：bad gap response 和移除 current GSV 都必须使 harness 判安全 gate 通过（即产品拒绝 mutant）。harness 不能 import 产品 Python 代码/fixture，只能控制标准库 fake OpenAI、外部 fake engine、CLI/HTTP/SQLite。
- [ ] clean 开发验证：
```powershell
uv sync --frozen --extra dev --python 3.11
uv lock --check
uv run python -m compileall -q src workers
uv run ruff format --check .
uv run ruff check .
uv run mypy src/voice_pipeline workers
uv run pytest tests -m 'not gpu and not gpu_residency and not quality_model' -vv -W error --strict-config --strict-markers --cov=voice_pipeline --cov=workers.indextts2 --cov-branch --cov-fail-under=85
uv run pytest .acceptance/batch3_blackbox/test_harness_self.py -q -W error
```
- [ ] 黑盒矩阵：A OpenAI JSON/key privacy；B sha/gap/overlap/vector/range 预引擎拒绝；C 中→日与中→英各分块/versions/final；D 短/长校正；E timeline/停顿/无尾停顿；F 引擎失败和 restart interrupted；G CLI HTTP/原子冲突；H 12 并发 background jobs + chapter max GPU=1；I 无真实 API/model/GPU 时为 `BLOCKED`，不得用 fake 宣称 PASS。
- [ ] 报告记录 commit、命令、exit codes、证据路径、每 Gate disposition 和实际 BLOCKED prerequisites；README 给出无密钥示例配置、request JSON、CLI/API/output layout。
- [ ] 提交 tracked docs：`git commit -m "docs: document full chapter synthesis workflow"`。

## Final self-review checklist

- [ ] OpenAI API、source interval 分块、vector/ref text、时长校正、批量 GSV、停顿/final/timeline、CLI/API 与独立验收均有独立任务和可执行测试。
- [ ] 没有 LLM 正文可替换原文，也没有任何路径绕过批次2 artifact/current/OCC 语义。
- [ ] 不使用真实资产的验收仅证明 fake 契约；真实模型、API credential、GPU 结果单独如实记录。
- [ ] 占位符扫描无匹配，所有后续接口均在前一任务定义。

