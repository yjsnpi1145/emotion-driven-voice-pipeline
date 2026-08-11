# Preserve Segment Source Slices Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use test-driven-development while implementing each task and verification-before-completion before reporting success.

**Goal:** 修复含换行或边界空白的 LLM 分块在落库时被误判为原文不一致的问题，同时保留严格的原文切片完整性校验。

**Architecture:** 在共享 schema 层增加“验证非空但保留原值”的文本类型，仅替换跨 Director、持久化请求和持久化记录的精确切片字段。存储层仍以章节原文和字符区间重新计算权威切片并做逐字符比较。

**Tech Stack:** Python 3.11、Pydantic v2、SQLite、Pytest、Ruff、Mypy、FastAPI。

## Global Constraints

- 不修改 LLM 返回协议；LLM 仍只负责字符区间和导演元数据。
- 不放宽 `SegmentStore` 的精确切片校验。
- 不对情绪参考文本、合成文本等普通输入引入保留空白语义。
- 先写回归测试并确认在旧实现上失败，再修改产品代码。
- `main` 受规则集保护，交付必须走分支、PR、CI、合并流程。

### Task 1: 建立失败回归

**Files:**
- Modify: `tests/unit/test_llm_director.py`
- Modify: `tests/integration_cpu/test_chapter_store.py`

**Steps:**
1. 添加包含换行和缩进边界的 DirectorPlan 单元测试。
2. 添加 ChapterStore 持久化集成测试，并断言所有分块重新拼接等于原文。
3. 运行两份测试，确认旧实现因边界空白被裁剪而失败。

### Task 2: 实现保真非空文本类型

**Files:**
- Modify: `src/voice_pipeline/models/schemas.py`
- Modify: `src/voice_pipeline/modules/llm/models.py`
- Modify: `src/voice_pipeline/models/persistence.py`

**Steps:**
1. 新增 `PreservedNonBlankText`，验证 `strip()` 后非空但返回原始字符串。
2. 只替换三个精确切片字段的类型。
3. 保持 SegmentStore 的严格相等判断不变。
4. 重跑定向测试确认转绿。

### Task 3: 完整验证与本地服务验证

**Steps:**
1. 运行 Ruff、Mypy、JavaScript 语法检查。
2. 运行完整非 GPU 测试套件。
3. 重新安装控制面并重启本地真实服务。
4. 验证 `/api/v1/health` 为 ready/real。

### Task 4: 受保护分支交付

**Steps:**
1. 提交变更并推送 `codex/preserve-segment-source-slices`。
2. 创建面向 `main` 的 PR。
3. 等待 `windows-python311` CI 通过。
4. squash 合并并同步本地 `main`。
5. 最终确认工作树干净且本地服务健康。
