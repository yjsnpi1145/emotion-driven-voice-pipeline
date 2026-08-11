# Reuse Chapter Base Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each behavior change and superpowers:verification-before-completion before delivery. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许分块参考音频局部重生成留空音色路径，并安全复用章节创建时冻结的总参考音色。

**Architecture:** API 接受可选覆盖路径，`SegmentRegenerationService` 负责把空值解析为章节私有快照中的总参考音色。`ChapterStore` 只读取路径和冻结哈希，服务层验证路径类型、存在性和 SHA-256 后再创建普通参考任务，执行器无需改变。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy asyncio、SQLite、原生 JavaScript、Pytest。

## Global Constraints

- 显式 `base_voice_path` 必须继续优先于章节回退值。
- 回退文件 SHA-256 必须等于 `chapter_runs.base_voice_sha256`。
- 公共章节、进度、SSE 和历史响应不得新增本地路径。
- GSV-only 请求不得读取或发送基础音色路径。
- `main` 继续通过受保护分支 PR 和 `windows-python311` CI 交付。

---

### Task 1: 建立空路径回归契约

**Files:**
- Modify: `tests/integration_cpu/test_segment_regeneration.py`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: `POST /api/v1/segments/{segment_id}/regenerate-reference`
- Consumes: `POST /api/v1/segments/{segment_id}/regenerate-both`
- Produces: 两个端点省略 `base_voice_path` 时仍返回 202 并最终成功的契约。

- [ ] **Step 1: 写失败集成测试**

把参考和 both 局部请求改为只发送 `request_id`，并从 `GET /api/v1/jobs/{job_id}` 断言冻结
请求中的 `base_voice_path` 等于章节创建时的绝对音色路径。

- [ ] **Step 2: 写 WebUI 契约**

```python
assert "留空则复用章节总参考音色" in script.text
assert "请先填写重新生成参考所用音色路径" not in script.text
```

- [ ] **Step 3: 验证旧实现失败**

Run: `uv run --python .venv-control/Scripts/python.exe pytest tests/integration_cpu/test_segment_regeneration.py tests/contract/test_workbench_api.py -q`

Expected: FAIL；API 对缺少 `base_voice_path` 返回 422，WebUI 仍要求填写。

### Task 2: 实现章节快照回退

**Files:**
- Modify: `src/voice_pipeline/models/persistence.py`
- Modify: `src/voice_pipeline/api/workbench_routes.py`
- Modify: `src/voice_pipeline/storage/chapter_store.py`
- Modify: `src/voice_pipeline/core/regeneration_service.py`
- Modify: `src/voice_pipeline/core/chapter_service.py`

**Interfaces:**
- Produces: `ChapterStore.base_voice_for_segment(segment_id: UUID) -> tuple[Path, str]`
- Produces: `SegmentRegenerationService._resolve_base_voice(segment_id: UUID, override: Path | None) -> Path`

- [ ] **Step 1: 允许省略覆盖路径**

```python
class SegmentReferenceRegenerationRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path | None = None
    activate_on_success: bool = True

class SegmentBothRegenerationRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path | None = None
    model_profile_id: UUID | None = None
```

- [ ] **Step 2: 从私有章节快照读取音色绑定**

`ChapterStore.base_voice_for_segment()` 联结 `chapter_run_segments` 和 `chapter_runs`，解析
`snapshot_json.request.base_voice_path`，返回 `Path` 和 `base_voice_sha256`。缺失映射抛
`KeyError`，损坏快照抛 `DATABASE_INTEGRITY_FAILED`。

- [ ] **Step 3: 解析并校验回退路径**

当 override 非空时原样返回；否则检查章节路径为绝对普通文件且非符号链接，并用
`sha256_file()` 验证冻结哈希。不满足时抛 `INVALID_INPUT`，消息提示选择覆盖音色。

- [ ] **Step 4: 新章节冻结绝对路径**

`ChapterService.submit()` 使用
`request.model_copy(update={"base_voice_path": base_voice})` 写入章节私有快照。

- [ ] **Step 5: 运行定向测试转绿**

Run: `uv run --python .venv-control/Scripts/python.exe pytest tests/integration_cpu/test_segment_regeneration.py -q`

Expected: PASS。

### Task 3: 更新 WebUI 默认行为

**Files:**
- Modify: `src/voice_pipeline/webui/app.js`
- Modify: `tests/contract/test_workbench_api.py`

**Interfaces:**
- Consumes: 可选 `base_voice_path` API。
- Produces: 留空回退、非空覆盖的局部生成请求。

- [ ] **Step 1: 更新文案**

输入框标签改为“可选覆盖音色路径”，placeholder 改为“留空则复用章节总参考音色”。

- [ ] **Step 2: 更新请求体**

```javascript
if (kind !== "gsv" && baseVoice) body.base_voice_path = baseVoice;
```

删除空路径的前端拒绝分支；GSV-only 请求仍不发送该字段。

- [ ] **Step 3: 运行 WebUI 契约和语法检查**

Run: `uv run --python .venv-control/Scripts/python.exe pytest tests/contract/test_workbench_api.py -q`
Run: `node --check src/voice_pipeline/webui/app.js`

Expected: 全部 PASS。

### Task 4: 完整验证和交付

**Files:**
- Verify: `src/voice_pipeline/`
- Verify: `tests/`

**Interfaces:**
- Consumes: 完整局部参考音色回退流程。
- Produces: 已安装本地服务和合并到 `main` 的修复。

- [ ] **Step 1: 运行静态与完整非 GPU 测试**

Run: Ruff、Mypy、两个 JavaScript 入口语法检查，以及
`pytest -q -m "not gpu and not gpu_residency and not quality_model"`。

Expected: 全部退出码为 0。

- [ ] **Step 2: 重装并重启服务**

Run: `scripts/setup-control.ps1` 和根目录 `启动服务.bat`。

Expected: `/api/v1/health` 为 `ready/real`，留空局部参考请求返回 202。

- [ ] **Step 3: 受保护分支交付**

推送 `codex/reuse-chapter-base-voice`，创建 PR，等待 `windows-python311` 后 squash 合并。

Expected: 本地 `main` 与 `origin/main` 一致，工作树干净，服务保持健康。
