# LLM Activity Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement each task test-first and verify RED before production edits.

**Goal:** 在章节进度条上方显示 LLM 当前工作状态、等待时间和可滚动的实际响应输出。

**Architecture:** 新建有界内存 `LlmActivityLog`，由 `RuntimeDirector` 记录操作生命周期、`OpenAiDirectorClient` 记录 HTTP 尝试和原始响应。FastAPI 暴露只读快照，Vanilla JS 定时拉取并安全渲染。

**Tech Stack:** Python 3.11、asyncio、httpx、FastAPI、Vanilla JavaScript、pytest。

## Global Constraints

- 保持非流式 OpenAI Chat Completions 协议。
- 缓冲区最多 80 条，单条内容最多 65,536 个字符。
- 禁止记录 Authorization、API Key、完整 prompt 和输入原文。
- 活动接口或 UI 故障不得阻断章节生成。
- 所有行为先写测试并观察预期失败。

---

### Task 1: 有界 LLM 活动日志

**Files:**
- Create: `src/voice_pipeline/modules/llm/activity.py`
- Create: `tests/unit/test_llm_activity.py`

**Interfaces:**
- `LlmActivityLog.emit(operation_id, operation, kind, message, content=None)`
- `LlmActivityLog.snapshot()` -> `LlmActivitySnapshot`

- [ ] 写失败测试，覆盖 started/terminal 的 active 派生、80 条保留上限、64 KiB 截断和 JSON 输出。
- [ ] 运行并确认模块不存在导致失败。
- [ ] 用 `deque(maxlen=80)`、`asyncio.Lock` 和严格 Pydantic 模型实现。
- [ ] 运行测试至通过。

### Task 2: RuntimeDirector 与 OpenAI 客户端埋点

**Files:**
- Modify: `src/voice_pipeline/modules/llm/client.py`
- Modify: `src/voice_pipeline/modules/llm/runtime.py`
- Modify: `src/voice_pipeline/modules/llm/fake.py`
- Modify: `tests/unit/test_llm_client.py`
- Modify: `tests/unit/test_runtime_llm_settings.py`

- [ ] 先写失败测试：请求等待时 snapshot.active 为 true；响应后含 raw JSON 和 completed；快照中不含 secret。
- [ ] 给内部 Director 调用传递 operation ID，RuntimeDirector 负责 started/completed/failed。
- [ ] OpenAI 客户端在请求发送、重试和响应到达时写入同一 activity log。
- [ ] FakeDirector 接受可选 operation ID 但不自行记录，保持统一生命周期。
- [ ] 运行 LLM 单元测试至通过。

### Task 3: 公开快照 API 与 WebUI 小窗

**Files:**
- Modify: `src/voice_pipeline/api/workbench_routes.py`
- Modify: `src/voice_pipeline/webui/index.html`
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `src/voice_pipeline/webui/styles.css`
- Modify: `tests/contract/test_workbench_api.py`

- [ ] 写失败契约测试，要求 `GET /api/v1/llm/activity`、`#llm-activity-console`、750 ms 轮询、安全文本渲染和滚动样式。
- [ ] 实现活动快照路由，仅访问 `plane.llm_client.activity`。
- [ ] 在进度条上方加入 status/header/log DOM，并在初始化时启动轮询。
- [ ] 使用 DOM `textContent` 渲染，工作时每秒刷新等待秒数；保留用户手动滚动位置。
- [ ] 更新静态资源版本，运行定向测试与 Node syntax check。

### Task 4: 回归和部署

**Files:**
- No production files expected.

- [ ] 运行 Ruff、mypy 和全部非 GPU 测试。
- [ ] 提交代码、构建当前提交 wheel 并安装到 `.venv-control`。
- [ ] 重启真实服务，确认 health ready 和活动接口可访问。
- [ ] 在 WebUI 验证小窗位于进度条上方、可滚动且控制台无错误。
