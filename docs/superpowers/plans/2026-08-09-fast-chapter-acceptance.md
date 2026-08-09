# Fast Chapter Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement each task test-first and verify the requested failure before production edits.

**Goal:** 让章节创建在 LLM 分块后立即返回，把 IndexTTS 时长探测移到后台参考阶段，并可靠复用探测缓存。

**Architecture:** `ChapterService.submit()` 保留同步 LLM 规划，随后持久化 run 并启动后台 `_run()`；后台逐段解析参考文本、用 OCC 更新 segment，再提交正式 reference/GSV jobs。`SynthesisService` 区分 cache hit/miss，原子输出层只对 Windows 短暂占用做有界重试。

**Tech Stack:** Python 3.11、FastAPI、asyncio、SQLite、pytest。

## Global Constraints

- 继续使用单 GPU consumer，不并行启动模型推理。
- 不改变公开 REST schema、数据库 schema 或 3–10 秒 GSV 参考窗口。
- 用户编辑优先；后台 LLM 修正不得覆盖 revision 已变化的草稿。
- 所有行为先写测试并观察 RED。

---

### Task 1: 冻结创建接口的快速返回契约

**Files:**
- Modify: `tests/contract/test_chapter_api.py`
- Modify: `src/voice_pipeline/core/chapter_service.py`
- Modify: `src/voice_pipeline/api/app.py`

- [ ] 增加受 gate 控制的 fake IndexTTS，断言 gate 未释放时 POST 已返回 `202` 且 run 可查询。
- [ ] 运行该测试并确认当前实现因同步探测超时失败。
- [ ] 给 `ChapterService` 注入 `SegmentStore`，从 `submit()` 移除同步 `_correct_reference_texts()`。
- [ ] 在 `_run()` 的每段 reference 提交前执行探测/修正；修正写回使用 `SegmentInputsPatch` 的 OCC revisions。
- [ ] OCC 冲突时重新读取 segment，保留用户最新文本。
- [ ] 释放 gate，断言章节完成且单段 IndexTTS 仅调用一次。

### Task 2: 缓存命中不重复发布

**Files:**
- Modify: relevant cache/pipeline tests under `tests/`
- Modify: `src/voice_pipeline/core/pipeline.py`

- [ ] 增加相同 reference 连续生成测试，对 `ArtifactStore.publish_blob()` 计数。
- [ ] 确认现实现第二次 cache hit 仍调用发布路径，测试失败。
- [ ] 在 `generate_reference()` 中显式记录 cache hit；仅 cache miss 执行 `_cache_put()`。
- [ ] 保留每个 job 的输出物化、版本记录和审计不变。

### Task 3: Windows 原子发布短暂占用重试

**Files:**
- Modify: `tests/unit/test_atomic_output.py`
- Modify: `src/voice_pipeline/modules/audio/atomic_output.py`

- [ ] monkeypatch `os.replace` 第一次抛出 `PermissionError`，断言发布最终成功。
- [ ] 运行并确认现实现失败。
- [ ] 加入有限退避重试，只捕获 `PermissionError`，不吞掉持续失败或其他异常。
- [ ] 运行原子输出与 artifact store 全部测试。

### Task 4: 回归、交付与本地验证

**Files:**
- No production files expected.

- [ ] 运行 Ruff、mypy 和定向测试。
- [ ] 运行全部非 GPU/质量模型测试。
- [ ] 提交代码，构建并安装当前提交 wheel。
- [ ] 重启本地服务，检查 `/api/v1/health`。
- [ ] 通过 WebUI 创建一个章节，确认 LLM 返回后立即出现历史记录，进度进入“参考音频”，无前台长时间假性“规划分块”。
