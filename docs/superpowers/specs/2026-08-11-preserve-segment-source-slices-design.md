# 分块原文切片保真修复设计

日期：2026-08-11
状态：设计通过，进入实施

## 1. 问题

LLM 只返回每个分块在章节原文中的 `source_start` 和 `source_end`。控制面随后用
`source_text[source_start:source_end]` 生成权威原文切片，并在写入 SQLite 前再次校验切片
必须逐字符一致。

现有共享 `NonBlankText` 类型在验证非空的同时会调用 `strip()` 并返回裁剪后的字符串。
当分块边界包含换行、缩进或其他首尾空白时，权威切片会在 Pydantic 模型构造过程中被
静默改变，最终触发：

`segment source_text must exactly match the task source slice [INVALID_INPUT]`

## 2. 决策

新增 `PreservedNonBlankText`：

- 用 `value.strip()` 只判断字符串是否为空白；
- 验证成功后返回原始 `value`，不改变任何字符；
- 只用于必须逐字符对应章节原文的分块切片字段；
- 普通用户输入、标题、参考文本与合成文本继续使用会规范化首尾空白的
  `NonBlankText`。

应用字段：

1. `MaterializedDirectedSegment.source_text`；
2. `CreateSegmentRequest.source_text`；
3. `SegmentRecord.source_text`。

存储层的严格相等校验保持不变。不能改成 `strip()` 后比较，否则会掩盖索引错位和文本
篡改。

## 3. 验收标准

1. 含换行和缩进边界的 DirectorPlan 物化后，每段文本与 Python 原始切片完全一致；
2. ChapterStore 能持久化这些分块，重新拼接所有 `source_text` 后与章节原文逐字符一致；
3. 纯空白分块仍被拒绝；
4. 现有非 GPU 测试、Ruff、Mypy 和前端 JavaScript 语法检查全部通过；
5. 修复经受保护分支 PR 的 Windows Python 3.11 CI 验证后合并。
