# 导演模式引号感知分析与可编辑配音文本设计

## 背景

导演模式目前把剧本按句末标点和固定字符数切成 LLM 分析单元。以下混合句会被作为一个整体交给 LLM：

```text
“我的初吻……”她慌乱地摆弄着手指，目光四处乱飘，“祥子，为什么——”
```

LLM 只能为整个单元返回一种类型，因此可能把引号外的动作描写一并识别为角色对白。与此同时，角色复核页显示的 `source_text` 是只读的；用户无法修正实际要进入翻译和配音的文字。若直接修改 `source_text`，又会破坏源文偏移、连续覆盖校验、拆分和审计能力。

## 目标

1. 在调用 LLM 前确定性识别成对引号，使对白与引号外叙述成为独立且连续的分析单元。
2. 保留导入原文和每个原始切片作为不可变审计依据。
3. 为每条语句增加独立的可编辑工作文本；后续翻译、中文情绪参考生成和最终配音都从该文本派生。
4. 保持现有角色拖拽、旁白开关、翻译复核、音色映射和生成流程不变。

## 非目标

- 不让 LLM 改写原始切片、源偏移或原文覆盖关系。
- 不实现通用文学引语语义判断；成对引号只提供本地结构约束，用户仍可调整角色和配音开关。
- 不在已进入翻译或生成阶段后修改工作文本；修改发生在 `role_review`。
- 不为既有已物化项目自动改写历史分析结果。新分析或重新分析会使用新规则。

## 一、确定性的引号感知分析单元

### 支持的引号

首版支持以下成对引号：

- 中文：`“…”`
- 日文：`「…」`
- 日文双层引号：`『…』`
- 成对英文双引号：`"…"`

扫描器使用栈匹配不同的左右引号；英文双引号在当前位置没有待关闭的英文引号时作为开启符，否则作为关闭符。只产出完整闭合的最外层引号跨度。未闭合或孤立的引号按普通字符处理，不能丢字、重排或扩展源范围。

### 单元上下文

`ScriptAnalysisUnit` 增加 `context` 字段，取值为：

- `general`：普通单元，由 LLM 分类。
- `quoted_dialogue`：完整引号跨度或其长度切片，服务端最终强制为对白并启用配音。
- `quote_bridge_narration`：同一行内两个完整引号跨度之间的非空文本，服务端最终强制为旁白；是否实际配音仍服从项目旁白开关。

引号跨度、桥接叙述和普通范围分别应用现有安全句界及 160 字上限。所有生成单元必须满足：

- 按顺序首尾相接；
- 完整覆盖 chunk；
- `source_text` 精确等于原文切片；
- 每个单元不超过 160 字；
- `unit_id` 稳定且顺序唯一。

示例必须切为：

```text
1. quoted_dialogue        “我的初吻……”
2. quote_bridge_narration 她慌乱地摆弄着手指，目光四处乱飘，
3. quoted_dialogue        “祥子，为什么——”
```

若两个引号之间包含换行，则该范围使用 `general`，避免把下一段的独立脚本格式强行判为旁白。未使用引号的 `甲：你好。` 等格式仍完全由 LLM 判断。

### LLM 与服务端职责

发送给 LLM 的每个单元包含 `unit_id`、`source_text` 和 `context`。提示词说明结构约束，但仍要求 LLM 为所有单元返回角色候选、别名和置信度。

本地物化时：

- `quoted_dialogue` 覆盖 LLM 的 `kind` 为 `dialogue`、`speak_enabled` 为 `true`，保留其角色候选和置信度；
- `quote_bridge_narration` 覆盖为 `narration`，清空临时角色和别名，`speak_enabled` 为 `true`；
- `general` 原样采用 LLM 分类。

这样既阻止动作描写再次合并进角色对白，也不妨碍 LLM 跨单元推断引号中的说话人。

### 缓存兼容

单元边界和提示词发生变化，分析缓存版本升级为：

- LLM fingerprint：`runtime-director-quote-units-v3`
- prompt version：`director-analysis-quote-units-v3`
- schema version：`3`

旧 v2 缓存必须 miss，避免复用曾经合并的对白单元。

## 二、不可变原文与可编辑工作文本

### 数据模型

`director_utterances` 增加非空列 `working_text`：

- 发布分析时初始化为 `source_text`；
- 迁移既有数据库时以 `source_text` 回填；
- `source_text`、`source_start`、`source_end` 继续不可通过 PATCH 修改；
- `working_text` 使用保留空白但拒绝全空白的文本类型。

`DirectorUtteranceRecord` 和公开 API 返回 `working_text`。`DirectorUtterancePatch` 接受 `working_text`，并继续使用 `expected_revision` 做乐观并发控制。

### 修改规则

工作文本只允许在项目状态为 `role_review` 时修改。成功修改后：

- 语句修订号和项目修订号各加一；
- 项目保持 `role_review`；
- 清空尚未生效的 `synthesis_text`、`ref_text_cn`、情绪向量及下游绑定字段，防止复用旧派生数据；
- 写入审计事件，但事件只记录修改字段，不复制全文。

全空白文本返回 `INVALID_INPUT`/422；过期修订返回 `VERSION_CONFLICT`/409；错误阶段返回 `DIRECTOR_STATE_CONFLICT`/409。

### 拆分与合并

源文结构操作仍基于不可变 `source_text`：

- “在原文光标处拆分”只读取只读原文区域的光标和绝对偏移；
- 当 `working_text != source_text` 时禁止拆分，避免把已经改写的文本错误映射到源偏移；前后端都执行此约束；
- 合并相邻语句时，新的 `source_text` 与 `working_text` 分别按左右顺序拼接；
- 拆分未编辑语句时，左右 `working_text` 分别初始化为对应的左右原文；
- 拆分或合并继续使角色确认失效，并清空下游派生字段。

## 三、后续翻译与配音数据流

完整数据链路为：

```text
不可变 source_text
        ↓ 初始化
可编辑 working_text
        ↓ LLM 翻译/同语种处理
synthesis_text + ref_text_cn + emotion_vector
        ↓ 用户翻译复核可继续微调
IndexTTS2 参考音频 + GPT-SoVITS 最终语音
```

`ScriptAnalysisService.translate()` 构造 `TranslationInput` 时必须使用 `working_text`，不能再使用 `source_text`。因此：

- 目标语言与原文不同时，编辑后的工作文本是新的翻译输入；
- 目标语言与原文相同时，编辑后的工作文本仍是同语种处理和最终合成文本的来源；
- 生成服务继续只读取经过翻译复核的 `synthesis_text`，保留现有生成快照和审计语义。

## 四、WebUI 交互

角色复核卡片中：

1. 主文本框显示“配音文本”，绑定 `working_text`，仅在 `role_review` 可编辑。
2. 输入发生变化后显示未保存状态和“保存配音文本”按钮。
3. 保存调用现有 utterance PATCH API，并携带当前 `expected_revision`。
4. 卡片提供折叠的“查看原始切片”区域，内部是只读 `source_text`；拆分按钮明确改名为“在原文光标处拆分”。
5. 当工作文本已修改时，原文拆分按钮禁用并说明原因。
6. 切换项目或点击“确认角色并生成翻译”时，若存在未保存的配音文本草稿，先阻止操作并提示保存，不能静默丢失或绕过修改。
7. 翻译复核页沿用当前可编辑 `synthesis_text` 表单。

工作文本草稿与翻译草稿分别维护，提示文案统一为“当前有未保存的修改”。

## 五、兼容性与验收

### 数据库

新增 Alembic `0005_director_working_text`。升级后既有语句的 `working_text` 必须与 `source_text` 完全一致；新数据库最终 revision 为 `0005_director_working_text`。

### 自动化验收

- 精确示例切成三个单元，类型上下文正确且所有字符、偏移连续。
- `“”`、`「」`、`『』`、成对 `""` 均受支持。
- 未闭合引号无损回退；长引号仍遵守 160 字上限。
- 恶意或错误 LLM 分类不能把桥接动作归入对白。
- 普通无引号脚本仍采用 LLM 分类。
- v2 缓存不复用，v3 缓存可命中。
- 迁移正确回填 `working_text`。
- PATCH 的阶段、空白和 OCC 规则正确，API 返回新字段。
- 翻译 mock 捕获到的输入是编辑后的 `working_text`。
- 拆分保护、合并语义和 WebUI 未保存草稿门禁均有回归测试。
- 完成定向测试、全量非 GPU/非破坏性测试、静态检查和构建验证。

### 运行验收

部署合并版本后验证：服务健康、数据库自动升级、导演页可编辑配音文本并查看原始切片。新建或重新分析的混合句应显示为两个对白卡片和一个旁白卡片；确认角色后，LLM 翻译活动使用保存后的配音文本。
