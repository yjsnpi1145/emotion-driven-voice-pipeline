# Chinese Quality Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for the behavior change and superpowers:verification-before-completion before delivery. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 防止 faster-whisper 返回繁体中文时，将内容正确的 IndexTTS2 参考音频误判为文本不匹配。

**Architecture:** `normalize_reference_text()` 继续负责唯一的文本规范化边界，但在 NFKC 和字符过滤后调用 OpenCC `t2s`。质量策略规范化版本升级到 2，真实分析器指纹加入 OpenCC 包版本，确保缓存键随算法变化。

**Tech Stack:** Python 3.11、OpenCC 1.4.1、RapidFuzz、Pydantic v2、Pytest、uv。

## Global Constraints

- 使用 OpenCC 官方 CPython 3.11 Windows wheel，不复制转换字典或自行实现简繁映射。
- `min_similarity=0.60` 与 `short_text_min_similarity=0.75` 保持不变。
- `normalizer_version` 必须从 1 升到 2。
- 生产故障样例必须先在旧实现上失败。

---

### Task 1: 建立生产回归测试

**Files:**
- Modify: `tests/unit/test_quality_policy.py`

**Interfaces:**
- Consumes: `evaluate_quality(...) -> QualityReport`
- Produces: 简体期望与繁体 ASR 内容可通过默认质量策略的回归契约。

- [ ] **Step 1: 写入失败测试**

```python
def test_quality_accepts_equivalent_simplified_and_traditional_chinese() -> None:
    report = evaluate_quality(
        total_seconds=5.6076,
        speech_seconds=5.6076,
        expected_text="明明我应该很生气，可被你这样托着，我却一句话都说不出来……",
        transcript="明明我應該很生氣可被你這樣拖著我卻一句話都說不出來",
        policy=QualityPolicy(),
    )
    assert report.passed is True
    assert report.normalized_text_similarity >= 0.60
```

- [ ] **Step 2: 验证旧实现失败**

Run: `uv run --python .venv-control/Scripts/python.exe pytest tests/unit/test_quality_policy.py::test_quality_accepts_equivalent_simplified_and_traditional_chinese -q`

Expected: FAIL，报告相似度为 `0.56`。

### Task 2: 接入 OpenCC 和策略版本

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/voice_pipeline/modules/quality/text.py`
- Modify: `src/voice_pipeline/modules/quality/models.py`
- Modify: `src/voice_pipeline/modules/quality/faster_whisper.py`
- Modify: `config/app.example.yaml`
- Modify: `config/open-source-reuse.yaml`
- Modify: `THIRD_PARTY_NOTICES.md`

**Interfaces:**
- Consumes: `opencc.OpenCC("t2s")`
- Produces: `normalize_reference_text(value: str) -> str` 的 v2 简体规范化输出。

- [ ] **Step 1: 锁定官方依赖**

Run: `uv add "OpenCC>=1.4.1,<1.5"`

Expected: `pyproject.toml` 和 `uv.lock` 锁定 OpenCC 1.4.1。

- [ ] **Step 2: 实现规范化并升级指纹**

```python
from opencc import OpenCC

_T2S = OpenCC("t2s")

def normalize_reference_text(value: str) -> str:
    filtered = "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum()
    )
    return _T2S.convert(filtered)
```

将 `normalizer_version` 固定为 `Literal[2] = 2`，并在 FasterWhisper 指纹中加入
`"opencc": version("OpenCC")`。

- [ ] **Step 3: 验证定向测试转绿**

Run: `uv run --python .venv-control/Scripts/python.exe pytest tests/unit/test_quality_policy.py -q`

Expected: PASS。

### Task 3: 完整验证和交付

**Files:**
- Verify: `src/voice_pipeline/modules/quality/`
- Verify: `tests/`

**Interfaces:**
- Consumes: 更新后的质量规范化及策略指纹。
- Produces: 可安装 wheel、健康的本地服务和已合并 PR。

- [ ] **Step 1: 运行静态和完整测试**

Run: Ruff、Mypy、JavaScript 语法检查及 `pytest -q -m "not gpu and not gpu_residency and not quality_model"`。

Expected: 全部退出码为 0。

- [ ] **Step 2: 重装并重启本地服务**

Run: `scripts/setup-control.ps1`，再使用根目录 `启动服务.bat`。

Expected: `/api/v1/health` 返回 `status=ready`、`mode=real`，策略指纹发生变化。

- [ ] **Step 3: 通过受保护分支交付**

Run: 推送 `codex/normalize-chinese-quality-text`、创建 PR、等待 `windows-python311`、squash 合并。

Expected: 本地 `main` 与 `origin/main` 一致且工作树干净。
