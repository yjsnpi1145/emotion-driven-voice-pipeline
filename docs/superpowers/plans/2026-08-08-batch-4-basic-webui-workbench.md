# 批次 4：基础 WebUI 工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个可从本地控制面打开的主从式 WebUI，使用户能提交批次 3 的整篇任务、查看全部分块、编辑草稿字段、试听当前音频，并通过 SSE 观察进度。

**Architecture:** 不引入 Node/React/外部 CDN；FastAPI 直接服务已打包的静态 HTML/CSS/ES-module JavaScript。UI 通过 `/api/v1` REST 获取数据和提交命令，`EventSource` 连接章节专属 SSE；列表采用固定高度窗口化渲染，避免长章节为每个分块挂载音频控件。批次 5 才把“重新生成参考/GSV/两者”、版本历史激活和显式重新拼接接入用户工作流。

**Tech Stack:** FastAPI/Starlette StaticFiles 与 StreamingResponse、原生 ES2022、HTML/CSS、现有 Pydantic/SQLite/HTTP API；不新增运行时前端依赖。

## Global Constraints

- UI 只能由 loopback FastAPI 控制面服务；任何 API key、绝对模型权重路径或服务端 artifact 路径不可渲染给浏览器。
- 单 GPU 约束保持不变；前端绝不直接访问 IndexTTS2/GPT-SoVITS worker。
- 分块原文只读；用户编辑 `synthesis_text`、中文参考文本、current vector、speed、pause、seed 时调用既有 `PATCH /api/v1/segments/{id}/inputs`，带双 revision OCC，成功后刷新记录；修改不自动推理。
- 情绪向量固定八维和顺序；总和实时显示。总和 `>0.8` 时显示错误并禁用未来参考生成入口；“恢复 LLM 值”仅填回 `llm_emotion_vector` 草稿；本批次没有隐式/显式模型调用。
- 当前 reference/GSV 音频只通过版本音频 endpoint 试听，浏览器没有 server filesystem 读取权限；历史列表/激活/局部重生成留给批次 5。
- SSE 只发送无密钥、无绝对路径的 `chapter_progress`/`heartbeat` JSON；断开连接不能取消后台任务。
- 静态资源必须进入 wheel；不使用外部字体、CDN 或在线遥测。
- 更新 `config/open-source-reuse.yaml`，记录原生浏览器/Starlette 复用和未采用 React/Vue 的理由。

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `src/voice_pipeline/storage/chapter_store.py` | 章节运行列表和每 run/segment job 映射，供 progress 查询。 |
| `src/voice_pipeline/core/chapter_service.py` | 提交 durable segment job 后持久化 job id。 |
| `src/voice_pipeline/api/workbench_routes.py` | 章节列表、公开 progress DTO、章节 SSE、UI 文件服务。 |
| `src/voice_pipeline/api/app.py` | 注册 workbench router、静态资源打包路径。 |
| `src/voice_pipeline/webui/index.html` | 顶栏、任务提交、左侧虚拟列表、右侧 editor 的无依赖页面骨架。 |
| `src/voice_pipeline/webui/app.js` | REST client、SSE、窗口化列表、OCC 编辑、播放器和错误状态。 |
| `src/voice_pipeline/webui/styles.css` | 本地可访问的主从布局与状态样式。 |
| `pyproject.toml` | Wheel package-data include。 |
| `tests/contract/test_workbench_api.py` | 静态页面、SSE JSON、隐私和列表 API 黑盒契约。 |
| `tests/unit/test_workbench_progress.py` | DTO 状态和 SSE heartbeat 无敏感字段测试。 |
| `tests/integration_cpu/test_webui_workbench.py` | fake chapter → 页面 API → patch → 音频播放 URL 链路。 |

### Task 1: 持久化可观察的章节 progress

**Files:** 修改 `storage/chapter_store.py`, `core/chapter_service.py`, `models/chapter.py`; 测试 `tests/integration_cpu/test_chapter_store.py`, `tests/unit/test_workbench_progress.py`。

**Interfaces:**
```python
class ChapterSegmentProgress(StrictModel):
    ordinal: int
    segment_id: UUID
    source_summary: str
    reference_job_status: JobStatus | None
    gsv_job_status: JobStatus | None
    active_ref_version_id: UUID | None
    active_gsv_version_id: UUID | None


async def set_segment_job(
    run_id: UUID, ordinal: int, kind: Literal["reference", "gsv"], job_id: UUID
) -> None: ...
async def progress(run_id: UUID) -> tuple[ChapterSegmentProgress, ...]: ...
```

- [ ] 写失败测试：reference/gsv job id 被持久化；progress 按 ordinal 返回、无 `base_voice_path`/model path；未开始 job 是 null。
- [ ] 运行 `uv run pytest tests/integration_cpu/test_chapter_store.py tests/unit/test_workbench_progress.py -q`，确认红。
- [ ] 在 ChapterService 每次成功 submit reference/GSV 后立即调用 `set_segment_job`，并通过 job store status + segment current pointer 组装 DTO；任何缺失 row 作为 DB integrity error。
- [ ] 运行 focused pytest、ruff、mypy；提交 `feat: expose durable chapter progress`。

### Task 2: Workbench REST、SSE 与打包静态入口

**Files:** 创建 `api/workbench_routes.py`; 修改 `api/app.py`, `pyproject.toml`; 测试 `tests/contract/test_workbench_api.py`。

**Public contract:**
```text
GET /                              -> index.html
GET /ui/{asset_path}               -> packaged static asset only
GET /api/v1/chapters               -> newest-first public chapter rows
GET /api/v1/chapters/{run_id}/progress -> public ordered segment progress
GET /api/v1/chapters/{run_id}/events -> text/event-stream
```

- [ ] 写失败 contract tests：`GET /` 包含 module JS；Traversal `/ui/../...` 为 404；章节列表与 progress 不含 `base_voice_path`、absolute artifact/model paths 或 API key；SSE 首帧 `event: chapter_progress`，可随后发送 `heartbeat`。
- [ ] 运行该 test，确认红。
- [ ] `ChapterStore.list_runs(limit=100)` 只取 public-safe fields；workbench router 用 `StreamingResponse` 每 500 ms 查询 progress、只在 canonical JSON 改变时发送 `chapter_progress`，每 15 s heartbeat；cancelled HTTP client 直接退出 generator。`GET /` 用 `FileResponse`，`/ui` 明确 allow-list `index.html/app.js/styles.css`。Hatch include `src/voice_pipeline/webui/**`。
- [ ] 运行 focused contract + mypy/ruff；提交 `feat: expose workbench progress and local ui`。

### Task 3: 无依赖主从式 WebUI

**Files:** 创建 `webui/index.html`, `webui/app.js`, `webui/styles.css`; 测试 `tests/contract/test_workbench_api.py`。

**UI behavior:**
```javascript
async function selectRun(runId) { /* fetch status + progress + task segments */ }
function renderVirtualRows(scrollTop) { /* render only visible ordinal range +/- 8 rows */ }
async function saveSegmentDraft(segment) { /* PATCH with expected revisions, then refetch */ }
function restoreLlmVector(segment) { /* no model request */ }
```

- [ ] 写失败 static contract tests：页面含 `#segment-list`, `#segment-editor`, `#chapter-form`; JS references `/api/v1/chapters`, `/progress`, `/events` and does not contain worker URLs, API key variable names or CDN URLs。
- [ ] 运行测试，确认红。
- [ ] 实现：顶部表单（title/source/base voice path/lang/model profile）先 `GET /api/v1/model-profiles`；提交章节后选择 run。左侧输入 search、状态筛选、固定 row height/window size；右侧只读 `source_text`，可编辑 `synthesis_text/ref_text_cn/speed/pause/seed`、八 slider 和 total；“保存草稿”和“恢复 LLM 值”。当前版本有 URL 时各渲染一个 `<audio controls preload="none">`。展示 version ID 和状态但不提供 batch5 command。
- [ ] SSE 更新顶部 run 状态/segment badges；SSE 断开时以 2秒 REST fallback 刷新，重新选择 run 时关闭旧 EventSource。
- [ ] 使用本地 CSS media query，在窄屏改为上下布局；keyboard focus 可见；文本和 status 有 aria label。
- [ ] 运行 static tests，提交 `feat: add master detail webui workbench`。

### Task 4: WebUI 编辑/API integration 与独立验收

**Files:** 创建 `tests/integration_cpu/test_webui_workbench.py`, `.acceptance/batch4_webui/run_acceptance.py` (ignored), `.acceptance/batch4_webui/test_harness_self.py` (ignored); 修改 `README.md`, `config/open-source-reuse.yaml`; 写 `runtime/handoff/batch4-*.json` (ignored)。

- [ ] 写失败 integration test：fake chapter completion后，UI public API reports two rows; PATCH current vector/Chinese ref/synthesis text increments correct revision without submission to engine; restore LLM client-side contract produces the LLM values; ready GSV URL responds with WAV.
- [ ] 运行红测，按现有 segment/version semantics 最小实现，验证绿。
- [ ] 开发验证：`uv sync --frozen --extra dev --python 3.11; uv lock --check; compileall; ruff format --check .; ruff check .; mypy src/voice_pipeline workers; pytest tests -m 'not gpu and not gpu_residency and not quality_model' --cov-fail-under=85`。
- [ ] 黑盒 harness 仅通过启动 control、HTTP、浏览器资源和 SQLite：创建 fake chapter，加载 `/`、SSE/REST progress、编辑草稿、确保无推理调用、验证播放器 URL；真实浏览器可视验收如资源/用户试听不足记 `BLOCKED`，不得将 fake 记为真实 UI/音频 PASS。
- [ ] README 写本地打开 `http://127.0.0.1:8765/`、不使用外网、工作台本批范围；reuse inventory 列 Starlette StaticFiles/StreamingResponse（BSD-3-Clause via FastAPI/Starlette）和原生 ES，拒绝 React/Vue（无 build/runtime 需求）。提交 tracked docs `docs: document local webui workbench`。

## Final self-review checklist

- [ ] 批次4的主从列表、编辑、试听、REST、SSE、状态显示均对应一项可执行测试。
- [ ] 草稿编辑绝不排队推理；批次5 command/history/compose 功能没有提前实现。
- [ ] 进度/API/HTML 不泄露敏感路径、API key 或 worker endpoint。
- [ ] wheel 安装后 `/` 的静态资源仍可访问；无 CDN/Node dependency。
