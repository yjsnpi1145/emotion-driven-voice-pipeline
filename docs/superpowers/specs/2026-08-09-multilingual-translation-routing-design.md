# 多语言翻译与中文参考路由设计

## 1. 目标

章节输入允许使用中文、日语、英语、韩语、粤语或其他自然语言。系统必须把每个分块明确拆成两条文本链路：

- `synthesis_text`：严格使用用户选择的 `target_language`，交给 GPT-SoVITS 生成最终配音。
- `ref_text_cn`：自然简体中文情绪短句，交给 IndexTTS2 生成中文参考音频，并作为 GPT-SoVITS 的中文 `prompt_text`。

输入 IndexTTS2 的文本永远不得直接使用日语、英语、韩语等非中文正文。

## 2. 已确认的使用场景

### 2.1 非中文原文，目标语言与原文相同

日语原文、日语目标时，每个分块的 `synthesis_text` 保留原意并使用日语；同一分块另外生成中文 `ref_text_cn`。IndexTTS2 只接收后者。

### 2.2 中文原文，目标语言为非中文

中文原文、日语目标时，所有源字符区间必须完整覆盖；每个区间生成对应的日语 `synthesis_text`。按顺序连接所有分块即构成整篇目标语言译文。中文原文片段可用于生成或改写 `ref_text_cn`，但 `synthesis_text` 不得继续沿用中文。

### 2.3 原文和目标语言均为非中文且不同

采用同一通用规则：源分块翻译成目标语言 `synthesis_text`，同时生成中文 `ref_text_cn`。例如英语原文、日语目标，形成“英语源片段 / 日语配音文本 / 中文参考文本”三字段快照。

### 2.4 中文原文、中文目标

不做目标语言转换；`synthesis_text` 保持对应中文源片段，`ref_text_cn` 仍可为适合 3--10 秒情绪参考的中文改写。

## 3. 方案比较与选择

### 方案 A：单次结构化导演调用同时完成分块、翻译与中文参考生成（采用）

`DirectorPlan` 的每个 `DirectedSegment` 直接返回 `synthesis_text` 和 `ref_text_cn`。源字符区间继续绑定原文，保证没有源内容被漏分；目标译文与中文参考天然与该区间对齐。优点是一次调用、低延迟、低成本且没有第二次分块造成的错位。

### 方案 B：先翻译整篇，再单独调用导演分块（不采用）

语义上直观，但需要至少两次大模型调用。第二次调用只能在译文上分块，必须额外建立译文区间与原文区间映射；长文本更容易发生错位，失败恢复也更复杂。

### 方案 C：先按原文分块，再逐块调用翻译（不采用）

对齐最明确，但每章产生大量串行 API 请求。现有 60 秒超时和重试策略会显著放大等待时间，也增加不同分块译风不一致的问题。

## 4. 数据模型

`DirectedSegment` 新增必填字段：

```text
synthesis_text: NonBlankText
```

字段含义：

| 字段 | 语言 | 用途 |
|---|---|---|
| `source_start/source_end` | 原文语言 | 定位并审计原始片段 |
| `source_text` | 原文语言 | 从原文区间本地物化，不由 LLM 重写 |
| `synthesis_text` | `target_language` | GPT-SoVITS 正文 |
| `ref_text_cn` | 简体中文 | IndexTTS2 正文及 GPT-SoVITS 中文提示文本 |

`MaterializedDirectedSegment` 只额外增加本地物化的 `source_text`，不得再用 `source_text` 覆盖 LLM 返回的 `synthesis_text`。

现有 SQLite `segments.synthesis_text`、`segments.ref_text_cn` 和版本输入快照已经能存储这两条链路，不增加数据库列或迁移。

## 5. LLM 契约

导演系统提示必须明确：

1. 自动识别源语言。
2. 每段都返回所有既有字段以及 `synthesis_text`。
3. `synthesis_text` 必须是该源区间的完整目标语言配音译文；源语言已等于目标语言时保持原文，不得摘要、解释或遗漏。
4. 所有分块按顺序共同覆盖整篇原文，不得漏译或重复。
5. `ref_text_cn` 必须是自然简体中文，表达同一段的情绪和语义，并适合生成 3--10 秒参考音频。
6. 即使源语言或目标语言为日语、英语、韩语或粤语，`ref_text_cn` 仍必须为普通话中文书面文本。

用户消息继续传递完整原文、SHA-256、Python 字符长度和目标语言。JSON Schema 由 Pydantic 模型生成，`synthesis_text` 为必填，缺失即返回 `LLM_INVALID_RESPONSE`。

## 6. 中文参考文本防线

共享 `ChineseReferenceText` 类型在解析 LLM 响应时执行最小确定性校验：

- 去除首尾空白后非空；
- 至少包含一个 CJK 统一表意文字；
- 不含日文平假名或片假名。

该校验用于阻止已观察到的“把日语正文直接送入 IndexTTS2”故障。它不尝试用字符集完全判断简体/繁体；自然性和简体要求仍由 LLM 契约、后续中文 ASR 质量门和用户试听共同保证。

手工工作台仍允许用户编辑 `ref_text_cn`，但使用相同类型校验，避免手工提交明显的日语或纯英文参考文本。

## 7. 运行流程

```text
用户原文 + target_language
→ LLM 单次生成带源区间的 DirectorPlan
→ 每段得到目标语言 synthesis_text + 中文 ref_text_cn
→ 校验源区间完整覆盖、双文本非空、中文参考字符约束
→ IndexTTS2(base_voice, ref_text_cn, emotion_vector)
→ 中文参考 WAV 质量门（3--10 秒、VAD、中文 ASR）
→ GPT-SoVITS(prompt_lang=zh, prompt_text=ref_text_cn,
             text=synthesis_text, text_lang=target_language)
→ 分块版本保存和最终拼接
```

## 8. UI

创建章节表单增加说明：

- “原文可为中文、日语、英语、韩语或其他语言”；
- “目标语言与原文不同时，系统自动生成目标语言配音文本”；
- “IndexTTS2 始终使用自动生成的中文情绪参考文本”。

分块编辑器继续同时展示原文、目标语言正文和中文参考文本，使用户可独立修正翻译与情绪参考，不引入新的操作步骤。

## 9. 错误和恢复

- LLM 缺少 `synthesis_text`、输出空文本或输出明显非中文 `ref_text_cn`：章节创建返回 `LLM_INVALID_RESPONSE`，不得启动 IndexTTS2。
- 目标语言翻译质量不满意：用户在分块工作台编辑 `synthesis_text`，只重新生成 GSV，不改变参考音频。
- 中文参考文本不满意：用户编辑 `ref_text_cn`，重新生成参考或两者。
- 任何失败沿用现有不可变版本、当前指针和迟到结果保护规则。

## 10. 验收标准

1. 日语原文、日语目标：Index 请求文本为中文，GSV 正文为日语。
2. 中文原文、日语目标：持久化的 `source_text` 为中文，`synthesis_text` 为 LLM 返回的日语译文，GSV 使用日语译文。
3. 英语原文、日语目标：保存英语源片段、日语正文和中文参考三字段。
4. LLM 返回日文假名或纯英文 `ref_text_cn` 时，在任何 GPU 调用前失败。
5. LLM 缺少 `synthesis_text` 时返回带字段路径的 `LLM_INVALID_RESPONSE`。
6. 中文原文、中文目标仍能正常运行。
7. 现有参考音频局部重生成、GSV 局部重生成、版本历史和最终拼接行为不回归。

## 11. 非目标

- 不引入独立第三方机器翻译服务。
- 不增加第二次整篇 LLM 翻译调用。
- 不在本批次实现术语表、翻译记忆、字幕文件导入或人工逐句对齐。
- 不改变 IndexTTS2 和 GPT-SoVITS 模型、权重或进程生命周期。
