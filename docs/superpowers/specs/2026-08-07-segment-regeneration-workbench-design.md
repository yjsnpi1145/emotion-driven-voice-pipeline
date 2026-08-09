# 分块级音频重生成工作台设计

日期：2026-08-07  
状态：设计已批准，按5个AI开发批次进入实施规划  
所属项目：Emotion Driven Cross-Language TTS Pipeline

## 1. 目标

在本地WebUI中列出长文本任务的全部分块，并允许用户对任意分块：

1. 查看和微调LLM生成的情绪向量；
2. 编辑中文情绪参考文本；
3. 重新生成IndexTTS2参考音频；
4. 在不调用IndexTTS2、不改变参考音频的情况下，重复生成GPT-SoVITS音频；
5. 一键顺序重新生成参考音频和GPT-SoVITS音频；
6. 试听、保留和切换两类音频的历史版本；
7. 完成多个分块调整后，显式重新拼接整篇成品。

## 2. 非目标

本功能不包含：

- 多用户和权限系统；
- 公网部署；
- Redis、Celery或分布式任务队列；
- 多GPU调度；
- 拖动情绪滑块时自动调用模型；
- 重新生成参考音频后自动调用GPT-SoVITS；
- 每次分块更新后自动重新拼接整篇。

## 3. 页面布局

采用主从式布局。

### 3.1 顶部任务栏

显示：

- 任务或章节名称；
- 总分块数和完成数量；
- 生成中、失败、GSV过期的数量；
- 当前整篇成品状态；
- “重新拼接整篇”按钮。

当任一分块的当前GSV版本发生变化或段后停顿变化时，整篇状态变为
“需要重新拼接”。

### 3.2 左侧分块列表

每一行显示：

- 分块编号；
- 文本摘要；
- 情绪标签；
- 当前参考音频版本；
- 当前GSV版本；
- 状态徽标。

状态至少包括：

- 完成；
- 等待生成；
- 参考参数未应用；
- GSV过期；
- 生成中；
- 失败。

列表支持搜索、状态筛选和虚拟滚动。

### 3.3 右侧分块编辑器

显示：

- 只读原文；
- 可编辑的GSV朗读文本；
- 8维情绪滑块；
- LLM基准向量及“恢复LLM值”；
- 中文参考文本；
- 当前参考音频播放器及版本入口；
- 当前GSV播放器及版本入口；
- 语速、段后停顿、seed和GSV采样参数；
- “重新生成参考音频”；
- “重新生成GSV”；
- “重新生成两者”。

## 4. 分块数据

每个分块至少保存：

```text
segment_id
source_start
source_end
source_text
synthesis_text
llm_emotion_vector
current_emotion_vector
ref_text_cn
speed_factor
pause_after_ms
seed
active_ref_version_id
active_gsv_version_id
ref_draft_revision
gsv_draft_revision
created_at
updated_at
```

`source_text`由程序按原文区间提取，不由LLM重写。

## 5. 情绪向量

顺序固定为：

```text
[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
```

规则：

- 正好8维；
- 单项范围0.0到1.0；
- 总和不超过0.8；
- `current_emotion_vector`初始复制`llm_emotion_vector`；
- 用户修改滑块只更新草稿，不触发模型；
- UI显示当前总和；
- 总和超过0.8时禁用参考生成；
- 提供显式的等比例归一化操作；
- 提供“恢复LLM值”。

## 6. 参考草稿与当前参考音频

以下概念必须分离：

```text
ref_draft
active_ref_version_id
```

`ref_draft`保存用户当前编辑的中文参考文本、情绪向量和seed。

修改`ref_draft`后：

- 显示“参考参数未应用”；
- 当前参考音频仍然有效；
- 当前参考音频文件和版本ID不变；
- 用户仍可使用当前参考音频生成GSV；
- 未应用的参考草稿不会影响该次GSV生成。

## 7. 三种生成命令

### 7.1 重新生成参考音频

输入为任务启动时的`ref_draft`快照。

流程：

```text
IndexTTS2生成
→ 音频解码检查
→ VAD有效语音时长检查
→ 3到10秒 GPT-SoVITS 参考窗口检查
→ ASR或文本对齐检查
→ 创建不可变参考版本
→ 将新版本设为当前参考
→ 标记当前GSV与参考不一致
```

该命令不调用GPT-SoVITS。

### 7.2 重新生成GSV

输入为：

```text
当前active_ref_version_id
当前synthesis_text
当前目标语言
当前GSV生成参数
当前seed
```

该命令：

- 不调用IndexTTS2；
- 不修改参考音频；
- 不应用`ref_draft`；
- 允许在`ref_draft`有未应用修改时执行；
- 在提交按钮附近明确显示使用的参考版本；
- 生成成功后创建不可变GSV版本并设为当前版本。

### 7.3 重新生成两者

流程：

```text
生成并检查新参考
→ 设为当前参考
→ 使用该参考生成GSV
→ 设为当前GSV
→ 标记整篇需要重新拼接
```

若参考生成失败，任务停止且不修改当前版本。

若参考成功但GSV失败：

- 新参考版本保留；
- 新参考仍设为当前；
- 原GSV继续可试听，但标记为过期；
- 用户可以只重试GSV阶段。

## 8. 状态和失效计算

| 事件 | 参考状态 | GSV状态 | 整篇状态 |
|---|---|---|---|
| 修改参考文本或向量 | 参数未应用，当前音频有效 | 保持原状态 | 保持原状态 |
| 新参考设为当前 | 当前参考最新 | 过期 | 保持旧成品 |
| 修改朗读文本或GSV参数 | 不变 | 过期 | 保持旧成品 |
| 修改段后停顿 | 不变 | 不变 | 需要重新拼接 |
| 新GSV设为当前 | 不变 | 最新 | 需要重新拼接 |
| 完成整篇拼接 | 不变 | 不变 | 最新 |

状态必须由版本ID、参数快照和revision计算，不能只依赖前端布尔值。

## 9. 版本模型

参考音频和GSV音频使用同一类不可变Artifact Version结构：

```json
{
  "version_id": "ref_v3",
  "artifact_type": "reference",
  "segment_id": 2,
  "audio_path": "artifacts/...",
  "content_sha256": "...",
  "created_at": "...",
  "input_snapshot": {},
  "model_fingerprint": {},
  "duration_seconds": 6.4,
  "quality_result": {}
}
```

每类普通历史保留最近5个版本。当前版本不参与自动清理。

版本历史提供：

- 试听；
- 查看参数；
- 设为当前；
- 恢复参数快照。

试听不会改变当前版本。

选择历史参考版本后，系统根据GSV版本记录的`ref_version_id`重新计算GSV状态。

选择历史GSV并执行“恢复并设为当前”后，恢复该版本的朗读文本和GSV参数，
并允许该版本参与整篇拼接。

已经生成的GSV是独立音频工件。将历史GSV设为当前后，即使它引用的参考版本
不是当前参考，它仍可参与整篇拼接；界面必须显示该GSV实际使用的参考版本。
只有下一次重新生成GSV时，才固定使用当时选中的`active_ref_version_id`。

## 10. 后端拓扑

实现遵循 GitHub 开源复用优先原则：上游官方模型/API 和成熟通用库以固定版本或 commit 直接依赖、薄封装并做契约测试；不复制上游源码或重写已有通用能力。本工作台特有的分块编排、版本绑定、单 GPU 安全边界和验收逻辑，以及落实这些边界所必需的薄 worker/adapter/安全包装可以自研，但内部继续复用成熟库。每批机器可读复用清单记录候选、SPDX 许可证、immutable pin、wrapper boundary 与取舍。

采用：

```text
WebUI
  ├── REST命令
  └── SSE进度事件
        ↓
FastAPI Orchestrator
        ↓
SQLite Job Store
        ↓
Single GPU Worker
  ├── IndexTTS2 Adapter
  └── GPT-SoVITS Adapter
        ↓
Local Immutable Artifact Store
```

IndexTTS2与GPT-SoVITS运行在独立Python环境和独立进程中。GPU任务并发为1。

## 11. 概念API

```text
GET    /api/tasks/{task_id}/segments
GET    /api/segments/{segment_id}
PATCH  /api/segments/{segment_id}/draft
POST   /api/segments/{segment_id}/regenerate-reference
POST   /api/segments/{segment_id}/regenerate-gsv
POST   /api/segments/{segment_id}/regenerate-both
GET    /api/segments/{segment_id}/reference-versions
GET    /api/segments/{segment_id}/gsv-versions
POST   /api/segments/{segment_id}/reference-versions/{version_id}/activate
POST   /api/segments/{segment_id}/gsv-versions/{version_id}/activate
POST   /api/tasks/{task_id}/compose
GET    /api/tasks/{task_id}/events
```

具体请求和响应Schema在实施计划阶段定义。

## 12. 任务一致性

任务创建时必须冻结：

- 输入参数快照；
- 当前参考版本ID；
- 分块revision；
- 模型及checkpoint指纹；
- seed；
- 输出音频规格。

任务结果提交时执行乐观并发检查。

若任务运行期间用户又修改参数或切换版本：

- 结果仍保存为历史版本；
- 结果不自动设为当前；
- UI提示存在“已完成但未启用”的新版本。

## 13. 错误处理

- IndexTTS2失败时不创建参考版本；
- GPT-SoVITS失败时不创建GSV版本；
- 旧版本不会被失败任务覆盖；
- 取消任务不删除已完成的Artifact；
- 应用重启后，`running`任务转为`interrupted`；
- `interrupted`任务允许重新提交；
- 错误记录阶段、错误码、摘要、诊断信息、重试次数和输入快照；
- 失败重试默认复用失败任务快照，用户也可按当前参数创建新任务。

## 14. 整篇拼接

整篇拼接只读取：

- 分块顺序；
- 每个分块的`active_gsv_version_id`；
- `pause_after_ms`；
- 最终输出音频规格。

不得使用“文件创建时间最新”的GSV。

当任一分块缺少有效当前GSV时，拼接命令拒绝执行，并返回阻塞分块列表。

拼接成功后保存时间轴Manifest，记录每个分块在成品中的开始、结束时间和
GSV版本ID。

## 15. 验收测试

1. 单独重新生成GSV时，参考文件SHA-256和版本ID完全不变。
2. `ref_draft`修改不阻止使用当前参考生成GSV。
3. GSV任务使用提交时明确显示的参考版本。
4. 参考重生成成功后不自动提交GSV任务。
5. 两者重生成严格按参考、质量检查、GSV执行。
6. 参考成功而GSV失败时，新参考保留且GSV可单独重试。
7. 任何失败都不会破坏此前可试听的版本。
8. 普通历史只保留最近5个，当前版本不清理。
9. 试听历史不改变当前选择。
10. 迟到任务结果不会覆盖用户后续选择。
11. GSV当前版本变化后，整篇状态变为需要重新拼接。
12. 拼接只使用明确选择的GSV版本。
13. 修改段后停顿不触发任何TTS生成。
14. 程序重启后，中断任务可以恢复或重试。
15. 已验证的中到日、中到英情绪传递样例作为GPU黄金回归测试。

## 16. 完成标准

当以上验收测试全部通过，并且用户可以在WebUI中完成以下闭环时，本功能
视为完成：

```text
选择分块
→ 微调LLM向量
→ 生成和试听参考
→ 固定参考并多次生成GSV
→ 从历史中选择满意版本
→ 调整其他分块
→ 重新拼接整篇
```
