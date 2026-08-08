# Batch 2 Reliable Jobs and Artifact Versions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏批次 1 双引擎、HTTP、CLI 和单 GPU 互斥契约的前提下，交付 SQLite 持久任务、不可变音频版本、当前版本指针、版本化缓存、参考音频质量门、取消/重试/重启恢复、迟到结果保护和安全历史清理。

**Architecture:** SQLite 是任务、分块、版本、指针、缓存索引和清理状态的唯一事实源；SQLAlchemy 2、Alembic 和 aiosqlite 提供持久化与迁移，内存 GPU 队列只保留“最终推理并发为 1”的安全职责。模型输出先在 job 目录生成并验证，再发布到内容寻址的不可变 Artifact Store，最后以一个短数据库事务提交版本、缓存索引、指针 CAS 和 job 终态；文件系统与 SQLite 之间采用“文件先发布、数据库最后提交、启动时协调”的确定性恢复协议。

**Tech Stack:** Python 3.11、FastAPI、Pydantic 2、SQLAlchemy 2.0、Alembic、aiosqlite、SQLite WAL、portalocker、faster-whisper（内含 Silero VAD）、RapidFuzz、soundfile、pytest、pytest-asyncio、HTTPX、PowerShell。

## Global Constraints

- 本项目仅供单机本地配音使用；所有 HTTP 服务继续只绑定 `127.0.0.1`，不加入公网部署、鉴权、多用户或分发功能。
- 批次 2 的实现基线必须是由主智能体独立验收为 `PASS` 的批次 1 最终 SHA；当前进行中的脏工作树不是实现基线。
- 控制面、IndexTTS2、GPT-SoVITS 仍使用三个互不相同的 Python 3.11 解释器；不得把任一模型包导入控制面。
- IndexTTS2 和 GPT-SoVITS 的源码、checkpoint、环境和运行配置指纹继续使用批次 1 固定值和锁文件。
- 所有 GPU 推理仍只能经过一个 `SerialGpuQueue` consumer；任何缓存、质量检查、取消、恢复或重试功能都不得引入第二条模型调用路径。
- `job_id` 仍是每次执行唯一、由服务端生成的 UUID；`request_id` 只做关联，相同 `request_id` 可重复提交且必须得到不同 `job_id`。
- 批次 1 的 `/api/v1/health`、三个 job POST、job/audio/manifest GET、shutdown API、202 envelope、CLI JSON 和错误 envelope 保持向后兼容。
- job 的兼容输出继续位于 `runtime/jobs/{job_id}`；批次 2 新增的内容寻址 blob 不得导致批次 1 job 下载 URL 失效。
- 所有公共路径必须是绝对本地路径；数据库和 Artifact Store 拒绝 UNC、网络文件系统、越界、目录和不受信任 symlink。
- 所有不可变文件继续采用同目录 partial、`flush + fsync`、`O_EXCL` 目标保留和原子发布；不得覆盖已有文件或 sentinel。
- SQLite 启用 `foreign_keys=ON`、`journal_mode=WAL`、`synchronous=FULL`、`busy_timeout=5000`、`wal_autocheckpoint=1000`。
- 不直接复制或删除活动数据库的 `-wal`/`-shm` 文件；数据库备份必须使用 SQLite backup API。
- 每个异步数据库事务使用独立 `AsyncSession`；不得在协程之间共享 Session；所有写事务再经过一个进程内写锁。
- job 状态固定为 `queued|running|succeeded|failed|cancelled|interrupted`；终态不可重新打开。
- retry 永远创建新 `job_id`，保留原冻结快照、原 `request_id`、`retry_of_job_id` 和递增的 `attempt`。
- 启动恢复把前一实例遗留的 `running` 原子转为 `interrupted`；`queued` 保持 queued 并按 FIFO 重新投递。
- 迟到结果必须保存为历史，但只有提交时捕获的 draft revision、当前指针和 selection revision 均未变化时才可自动启用。
- 每个 segment、每种 artifact type 保留“当前版本 + 最近 5 个其他普通 ready 版本”；被存活 GSV 引用的参考版本和被 queued/running job 捕获的版本在配额外保护。
- 情绪向量继续是 8 个有限浮点数、每项 `0..1`、总和 `<=0.8`；缓存中的“normalized”仅表示规范化序列化，绝不对数值做隐藏缩放。
- 负 seed 或任何随机采样模式不命中、不写入合成缓存。
- 参考音频真实质量门固定包含 WAV、VAD、`3..9` 秒窗口和中文 ASR 文本相似度；真实模式不得静默降级为 fake 或跳过。
- GitHub/上游复用优先：SQLAlchemy、Alembic、aiosqlite、portalocker、faster-whisper/Silero VAD、RapidFuzz 直接锁版本复用；只自研项目特有的状态机、OCC、双阶段发布、版本绑定、缓存 key、安全清理和独立验收。
- 不复制上游源码改名；所有采用/拒绝候选、SPDX、immutable pin、wrapper boundary 和锁文件证据写入 `config/open-source-reuse.yaml`。
- 批次 2 不实现 OpenAI 兼容 LLM、自动全文分块、批量全文生成、停顿拼接、`final.wav`、WebUI、SSE 或完整的三种工作台重生成编排。
- 批次 2 只交付批次 5 所需的底层 segment CRUD、低层 reference/GSV job、版本列表/激活和 OCC；“重新生成两者”、草稿恢复、过期 UI、试听 UI 和整篇重拼仍属于批次 5。
- 所有开发遵循测试先行；每个任务先看到目标测试因缺失行为失败，再写最小实现，再运行局部和回归测试，再提交。

---

## 0. 设计依据、方案选择与批次边界

### 0.1 规范依据

- `D:\TTSsystem\Emotion_Driven_TTS_Pipeline_Design.md:342-375`：reference/GSV 缓存 key。
- `D:\TTSsystem\Emotion_Driven_TTS_Pipeline_Design.md:517-542`：不可变版本、最近 5 版、当前保护。
- `D:\TTSsystem\Emotion_Driven_TTS_Pipeline_Design.md:555-588`：SQLite、任务状态、快照、迟到保护、恢复。
- `D:\TTSsystem\Emotion_Driven_TTS_Pipeline_Design.md:666-680`：批次 2 冻结范围和验收目标。
- `D:\TTSsystem\docs\superpowers\specs\2026-08-07-segment-regeneration-workbench-design.md:86-108`：segment 最小字段。
- 同一规格 `:227-264`：Artifact Version、GSV→reference 绑定和历史选择。
- 同一规格 `:309-337`：输入快照、OCC、迟到结果、错误和 retry。
- `D:\TTSsystem\docs\superpowers\plans\2026-08-07-batch-1-dual-engine-core.md:175-240`：批次 1 HTTP、job ID、request ID 和 job 目录契约。

### 0.2 已比较的实现方案

| 能力 | 方案 | 结论 |
|---|---|---|
| SQLite 访问 | stdlib `sqlite3` + 手写迁移 | 拒绝；会重复实现迁移、映射、约束和事务模板 |
| SQLite 访问 | SQLModel + Alembic | 拒绝；会把现有 frozen Pydantic API model 与 ORM model 耦合 |
| SQLite 访问 | SQLAlchemy 2 + Alembic + aiosqlite | 采用；领域 model/ORM 分离、迁移成熟、异步 FastAPI 边界清晰 |
| 任务队列 | Celery/RQ/Redis | 拒绝；单机本地和单 GPU 不需要分布式基础设施 |
| 任务队列 | SQLite durable dispatcher + 现有 GPU queue | 采用；SQLite 负责恢复，GPU queue 只负责并发 1 |
| 通用缓存 | DiskCache | 拒绝；独立 SQLite 和淘汰语义不能原子保护 current/GSV 父引用 |
| 缓存 | 现有数据库中的 cache index + Artifact Store | 采用；key、引用保护、清理和版本处于同一事实源 |
| VAD | WebRTC VAD | 拒绝；只有 VAD，仍需另一套 ASR/文本一致性依赖 |
| 对齐 | WhisperX | 拒绝；强制对齐和 diarization 对 3–9 秒中文参考过重 |
| VAD/ASR | faster-whisper + 内置 Silero VAD | 采用；一个锁定栈完成 CPU VAD 与中文 ASR，且无需系统 FFmpeg |
| 文本度量 | 自写 Levenshtein | 拒绝；重复通用能力 |
| 文本度量 | RapidFuzz | 采用；只自研项目特有的 Unicode 规范化和阈值策略 |
| 单实例 | 自写 Windows `msvcrt` 锁 | 拒绝；平台细节重复 |
| 单实例 | portalocker | 采用；持有 `runtime/state/control.lock` 的跨平台 OS 锁 |

### 0.3 批次边界

批次 2 必须建立并测试：

1. narration task/segment 的 SQLite schema 和 repository；
2. persistent generation job、durable dispatcher、cancel、retry、restart recovery；
3. 不可变 artifact blob/version、job artifact 关系、当前指针 CAS；
4. reference/GSV cache key、cache hit、corrupt cache fail-closed；
5. WAV/VAD/中文 ASR/文本相似度质量报告；
6. 迟到结果只进历史；
7. current、父 reference、在途 job 保护下的最近 5 版清理；
8. 最小 loopback API/CLI，使上述行为可被独立黑盒验收。

批次 3 才实现 LLM 和全文流水线；批次 4 才实现 WebUI/SSE；批次 5 才实现面向用户的三种完整重生成编排、历史参数恢复和整篇重拼。

---

## 1. 冻结公共契约

### 1.1 兼容 API

以下路由必须保留原方法、路径和成功/失败 envelope：

```text
GET  /api/v1/health
POST /api/v1/jobs/reference
POST /api/v1/jobs/gsv
POST /api/v1/jobs/segment
GET  /api/v1/jobs/{job_id}
GET  /api/v1/jobs/{job_id}/audio/reference
GET  /api/v1/jobs/{job_id}/audio/target
GET  /api/v1/jobs/{job_id}/manifest/reference
GET  /api/v1/jobs/{job_id}/manifest/run
POST /api/v1/control/shutdown
```

`GET /api/v1/jobs/{job_id}` 只做向后兼容的字段追加：

```json
{
  "retry_of_job_id": null,
  "attempt": 1,
  "cancel_requested_at": null,
  "activation_outcome": "not_applicable",
  "artifact_version_ids": [],
  "cache": {
    "reference_hit": false,
    "gsv_hit": false
  }
}
```

### 1.2 批次 2 新增 API

```text
POST   /api/v1/jobs/{job_id}/cancel
POST   /api/v1/jobs/{job_id}/retry

POST   /api/v1/tasks
GET    /api/v1/tasks/{task_id}
POST   /api/v1/tasks/{task_id}/segments
GET    /api/v1/tasks/{task_id}/segments
GET    /api/v1/segments/{segment_id}
PATCH  /api/v1/segments/{segment_id}/inputs

POST   /api/v1/segments/{segment_id}/jobs/reference
POST   /api/v1/segments/{segment_id}/jobs/gsv
GET    /api/v1/segments/{segment_id}/versions
POST   /api/v1/segments/{segment_id}/versions/{version_id}/activate
GET    /api/v1/versions/{version_id}
GET    /api/v1/versions/{version_id}/audio

POST   /api/v1/maintenance/retention/plan
POST   /api/v1/maintenance/retention/{plan_id}/apply
GET    /api/v1/maintenance/cache
```

所有新增路由只允许 loopback。批次 2 的 segment job 是低层持久化命令，不提供 `regenerate-both`；批次 5 在这些原语之上增加工作台命令。

### 1.3 新增请求语义

`POST /api/v1/tasks`：

```json
{
  "title": "第一章",
  "source_text": "私はまだ生きている。",
  "target_language": "ja",
  "output_spec": {
    "format": "wav",
    "sample_rate": 32000,
    "channels": 1,
    "sample_width_bits": 16
  }
}
```

`POST /api/v1/tasks/{task_id}/segments`：

```json
{
  "ordinal": 0,
  "source_start": 0,
  "source_end": 12,
  "source_text": "私はまだ生きている。",
  "synthesis_text": "私はまだ生きている。",
  "llm_emotion_vector": [0.0, 0.0, 0.20, 0.0, 0.0, 0.25, 0.0, 0.15],
  "ref_text_cn": "我仍然活着。",
  "speed_factor": 0.95,
  "pause_after_ms": 500,
  "seed": 1234
}
```

服务验证 source slice，并把 `llm_emotion_vector` 原样复制为初始 `current_emotion_vector`；请求不能另传一套初始 current。

`POST /api/v1/jobs/{job_id}/retry`：

```json
{
  "mode": "frozen_snapshot"
}
```

只接受 `failed|cancelled|interrupted`；原 job 不变，新 job 使用同一 `request_id` 和原 snapshot。

`PATCH /api/v1/segments/{segment_id}/inputs`：

```json
{
  "expected_ref_draft_revision": 3,
  "expected_gsv_draft_revision": 7,
  "ref_text_cn": "我仍然活着。",
  "current_emotion_vector": [0.0, 0.0, 0.20, 0.0, 0.0, 0.25, 0.0, 0.15],
  "synthesis_text": "私はまだ生きている。",
  "speed_factor": 0.95,
  "pause_after_ms": 500,
  "seed": 1234
}
```

未提供的可编辑字段保持原值。修改 reference 字段或 seed 增加 `ref_draft_revision`；修改 GSV 字段或 seed 增加 `gsv_draft_revision`；只改 `pause_after_ms` 不增加两者，但增加 task/segment 通用 revision。

低层 reference job：

```json
{
  "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
  "base_voice_path": "D:\\voices\\character.wav",
  "activate_on_success": true
}
```

低层 GSV job：

```json
{
  "request_id": "4de7ed6a-00f0-4be6-b916-1f10cf96019e",
  "activate_on_success": true
}
```

GSV job 必须从提交瞬间的 `active_ref_version_id` 读取音频、中文 prompt 和 hash，不读取未应用的后续编辑值。

激活历史：

```json
{
  "expected_selection_revision": 9
}
```

跨 segment、错误 artifact type、非 ready、缺失文件、过期 revision 都返回 HTTP 409，且 current 完全不变。

### 1.4 状态与取消线性化

合法状态转换：

```text
queued  -> running | cancelled
running -> succeeded | failed | cancelled | interrupted
terminal -> no transition
```

取消语义：

- queued：数据库 CAS 为 cancelled；dispatcher 即使已经读到 ID 也不能 claim。
- running：先写 `cancel_requested_at`，再取消对应执行 task；引擎 abort 并确认 active=0 后才写 cancelled。
- abort 无法确认：queue 维持 poisoned，job 写 failed/`ENGINE_UNAVAILABLE`，不得谎报 cancelled。
- success 事务先提交：cancel 返回当前 succeeded。
- cancel 标记先提交：后续 success 事务不得更新 current；已经完成并发布的 artifact 可保留为 history/cache。
- 已 cancelled 再 cancel 幂等返回 200；其他终态返回 409 和当前终态。

### 1.5 迟到结果 CAS

reference 自动激活同时要求：

```text
segment.ref_draft_revision == snapshot.ref_draft_revision
segment.active_ref_version_id == snapshot.active_ref_version_id
segment.selection_revision == snapshot.selection_revision
job.cancel_requested_at IS NULL
```

GSV 自动激活同时要求：

```text
segment.gsv_draft_revision == snapshot.gsv_draft_revision
segment.active_ref_version_id == snapshot.active_ref_version_id
segment.active_gsv_version_id == snapshot.active_gsv_version_id
segment.selection_revision == snapshot.selection_revision
job.cancel_requested_at IS NULL
```

CAS 失败时 job 仍可 succeeded，version 仍 ready，但 `activation_outcome=history_only`。

---

## 2. 持久化与 Artifact 模型

### 2.1 SQLite 表

首个 Alembic revision 固定创建：

```text
dubbing_tasks
segments
generation_jobs
artifact_blobs
artifact_versions
artifact_version_state
job_artifacts
cache_entries
quality_cache_entries
retention_plans
retention_candidates
instance_recovery_runs
alembic_version
```

关键字段：

```text
dubbing_tasks
  task_id UUID PK
  title TEXT
  source_text TEXT
  source_text_sha256 CHAR(64)
  target_language TEXT
  output_spec_json TEXT
  revision INTEGER >= 0
  created_at_utc, updated_at_utc

segments
  segment_id UUID PK
  task_id FK dubbing_tasks ON DELETE RESTRICT
  ordinal INTEGER
  source_start, source_end INTEGER
  source_text, synthesis_text, target_language TEXT
  llm_emotion_vector_json, current_emotion_vector_json TEXT
  ref_text_cn TEXT
  speed_factor REAL
  pause_after_ms INTEGER
  seed INTEGER
  ref_draft_revision, gsv_draft_revision, selection_revision, revision INTEGER
  active_ref_version_id UUID NULL
  active_gsv_version_id UUID NULL
  created_at_utc, updated_at_utc
  UNIQUE(task_id, ordinal)

generation_jobs
  job_id UUID PK
  request_id UUID NOT UNIQUE
  kind reference|gsv|segment
  status queued|running|succeeded|failed|cancelled|interrupted
  stage TEXT
  task_id, segment_id UUID NULL
  retry_of_job_id UUID NULL
  attempt INTEGER >= 1
  request_snapshot_json TEXT
  request_snapshot_sha256 CHAR(64)
  ref_draft_revision_snapshot, gsv_draft_revision_snapshot INTEGER NULL
  active_ref_version_id_snapshot, active_gsv_version_id_snapshot UUID NULL
  selection_revision_snapshot INTEGER NULL
  model_fingerprint_json, output_spec_json TEXT
  cancel_requested_at_utc NULL
  runner_instance_id UUID NULL
  result_json, error_json TEXT NULL
  activation_outcome TEXT
  created_at_utc, started_at_utc, finished_at_utc

artifact_blobs
  content_sha256 CHAR(64) PK
  relative_path TEXT UNIQUE
  byte_size, frames, sample_rate, channels INTEGER
  duration_seconds, rms_dbfs, peak_dbfs REAL
  lifecycle_state ready|deleting|deleted|missing|corrupt
  created_at_utc, checked_at_utc

artifact_versions
  version_id UUID PK
  segment_id UUID NULL
  artifact_type reference|gsv
  display_ordinal INTEGER NULL
  source_job_id UUID
  blob_sha256 CHAR(64)
  manifest_relative_path TEXT UNIQUE
  ref_version_id UUID NULL
  ref_content_sha256 CHAR(64) NULL
  input_snapshot_json, input_snapshot_sha256 TEXT
  model_fingerprint_json, model_fingerprint_sha256 TEXT
  quality_profile_version TEXT
  quality_result_json TEXT
  complete_cache_key CHAR(64) NULL
  created_at_utc
  UNIQUE(segment_id, artifact_type, display_ordinal)

artifact_version_state
  version_id UUID PK
  state ready|deleting|deleted|missing|corrupt
  diagnostic_json TEXT
  checked_at_utc

job_artifacts
  job_id UUID
  version_id UUID
  role reference|target
  stage_index INTEGER
  PRIMARY KEY(job_id, version_id, role)

cache_entries
  cache_key CHAR(64) PK
  kind reference_audio|gsv_audio
  canonical_payload_json TEXT
  blob_sha256 CHAR(64)
  source_version_id UUID NULL
  state valid|invalid
  created_at_utc, last_hit_at_utc
  hit_count INTEGER

quality_cache_entries
  cache_key CHAR(64) PK
  audio_sha256 CHAR(64)
  expected_text_sha256 CHAR(64)
  policy_fingerprint_sha256 CHAR(64)
  report_json TEXT
  state valid|invalid
  created_at_utc, last_hit_at_utc

retention_plans
  plan_id UUID PK
  storage_revision INTEGER
  status planned|applying|completed|failed
  scope_json, summary_json TEXT
  created_at_utc, applied_at_utc

retention_candidates
  plan_id UUID
  version_id UUID
  artifact_type reference|gsv
  reason TEXT
  action keep|delete
  protection_reason TEXT NULL
  ordinal INTEGER
  PRIMARY KEY(plan_id, version_id)

storage_meta
  singleton_id INTEGER PK CHECK(singleton_id = 1)
  protected_graph_revision INTEGER >= 0
```

`artifact_versions` payload字段使用 SQLite trigger 禁止 UPDATE；生命周期单独写入 `artifact_version_state`。历史清理保留 tombstone 元数据，只删除不再受保护的 blob/manifest 文件。
`segments.active_ref_version_id/active_gsv_version_id` 使用命名 FK `ON DELETE RESTRICT`；activation trigger 再验证目标属于同一 segment、artifact type 正确且 state=ready。GSV insert trigger 验证 `ref_version_id` 是同一 segment 的 ready reference，并把当时 ref SHA 永久固化。

`storage_meta.protected_graph_revision` 只在 version/state、segment current、queued/running snapshot protection 或 cache validity 改变时增加；创建 retention plan 本身不增加。plan 保存该 revision，apply 用它拒绝过期保护图。

### 2.2 Artifact Store 布局

```text
runtime/
├── jobs/{job_id}/                         # 批次 1 兼容输出
├── state/pipeline.sqlite3
├── state/control.lock
├── artifacts/
│   ├── blobs/sha256/ab/{64-char-sha}.wav
│   ├── manifests/{version_id}.json
│   ├── staging/{job_id}/{random}.partial
│   ├── trash/{retention-plan-id}/
│   └── quarantine/{recovery-run-id}/
├── models/faster-whisper-small/
├── backups/
└── logs/{instance_id}/
    ├── engine-audit.jsonl
    └── state-audit.jsonl
```

数据库只存相对 `runtime` 的路径。任何读取都通过 `resolve_runtime_relative()`，要求解析后仍在允许根内且最终文件不是 symlink。

### 2.3 文件/数据库提交协议

固定顺序：

1. job row 已 queued 并提交；
2. dispatcher CAS `queued -> running`；
3. 批次 1 service 在 `runtime/jobs/{job_id}` 生成兼容文件；
4. WAV/VAD/ASR 质量通过；
5. 将目标复制到 `artifacts/staging`，flush、fsync、重算 SHA；
6. O_EXCL 发布 blob 和 version manifest；若 blob 已存在，验证 size/hash 后复用；
7. 短数据库事务插入 blob/version/job_artifacts/cache，执行 current CAS，写 job result 和 succeeded；
8. transaction commit 后才对 API 报告 succeeded。

崩溃恢复：

- 第 5 步前崩溃：只有 job partial；running 转 interrupted。
- 第 6 步后、第 7 步前崩溃：只有 orphan 文件；启动协调器移入 quarantine。
- 第 7 步后崩溃：DB 只引用已发布文件。
- 启动协调器完成前，HTTP 不开始接单。

### 2.4 Cache key

规范 JSON：

```python
json.dumps(
    payload,
    ensure_ascii=False,
    allow_nan=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

reference audio key 必含：

```text
schema_version
base_voice_content_sha256
ref_text_cn
emotion_vector 的原值规范 JSON
Index engine fingerprint 全字段
inference parameters
seed
output WAV spec
序列化后的有效 upstream request（包含 adapter 填入的所有默认值）
```

GSV audio key 必含：

```text
schema_version
reference_binding_hash(
  reference_audio_sha256,
  ref_text_cn,
  prompt_lang
)
synthesis_text
target_language
GPT/SoVITS engine fingerprint 全字段
speed_factor
sampling parameters
seed
output WAV spec
序列化后的有效 upstream request（包含 adapter 填入的所有默认值）
```

quality key 必含：

```text
audio_sha256
expected_ref_text_sha256
quality_policy_fingerprint
```

缓存命中仍须验证路径、非 symlink、文件 SHA 和 WAV probe；失败则把 cache entry 标 invalid，再执行真实引擎，不得向用户返回损坏文件。

### 2.5 质量策略 v1

真实 reference 质量策略固定为：

```text
total_duration: 3.0 <= seconds <= 9.0
speech_duration_seconds >= 1.5
speech_ratio >= 0.35
ASR language forced to zh
normalized_text_similarity >= 0.60
expected normalized length < 4 时 similarity >= 0.75
```

文本规范化：

1. Unicode NFKC；
2. `casefold()`；
3. 删除 Unicode punctuation、separator 和 control；
4. 保留汉字、字母和数字；
5. 不做简繁转换、不改写语义。

`QualityReport` 必须保存 observed、threshold、pass/fail、ASR transcript、规范化文本、speech timestamps、模型/策略指纹和稳定错误码。VAD/ASR 失败不创建 artifact version，不改变 current。

质量模型固定：

```text
repository: Systran/faster-whisper-small
revision: 536b0662742c02347bc0e980a01041f333bce120
device: cpu
compute_type: int8
language: zh
beam_size: 5
condition_on_previous_text: false
vad_filter: true
```

模型本体保持 Git ignored。tracked lock 的 v1 内容固定为：

```yaml
schema_version: 1
repository: Systran/faster-whisper-small
revision: 536b0662742c02347bc0e980a01041f333bce120
license_spdx: MIT
files:
  - path: config.json
    size: 2370
    sha256: b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828
  - path: model.bin
    size: 483546902
    sha256: 3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671
  - path: tokenizer.json
    size: 2203239
    sha256: fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab
  - path: vocabulary.txt
    size: 459861
    sha256: 34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913
```

setup 脚本只消费并验证该 tracked lock，不在普通安装时改写它。

---

## 3. 文件职责映射

### 新建

```text
src/voice_pipeline/models/persistence.py
src/voice_pipeline/core/state_machine.py
src/voice_pipeline/core/dispatcher.py
src/voice_pipeline/core/job_executor.py
src/voice_pipeline/storage/__init__.py
src/voice_pipeline/storage/database.py
src/voice_pipeline/storage/orm.py
src/voice_pipeline/storage/job_store.py
src/voice_pipeline/storage/segment_store.py
src/voice_pipeline/storage/version_store.py
src/voice_pipeline/storage/cache_store.py
src/voice_pipeline/storage/artifact_store.py
src/voice_pipeline/storage/recovery.py
src/voice_pipeline/storage/retention.py
src/voice_pipeline/storage/migrations/__init__.py
src/voice_pipeline/storage/migrations/env.py
src/voice_pipeline/storage/migrations/script.py.mako
src/voice_pipeline/storage/migrations/versions/0001_batch2_foundation.py
src/voice_pipeline/modules/quality/__init__.py
src/voice_pipeline/modules/quality/models.py
src/voice_pipeline/modules/quality/ports.py
src/voice_pipeline/modules/quality/fake.py
src/voice_pipeline/modules/quality/faster_whisper.py
src/voice_pipeline/modules/quality/text.py
src/voice_pipeline/modules/cache/__init__.py
src/voice_pipeline/modules/cache/keys.py
src/voice_pipeline/api/foundation_routes.py
src/voice_pipeline/api/maintenance_routes.py
src/voice_pipeline/runtime/state_audit.py
config/quality-model.lock.yaml
docs/batch-2-open-source-reuse.md
docs/batch-2-storage-runbook.md
scripts/setup-quality.ps1
scripts/verify-batch2.ps1
tests/unit/test_persistence_models.py
tests/unit/test_job_state_machine.py
tests/unit/test_cache_keys.py
tests/unit/test_quality_policy.py
tests/unit/test_artifact_store.py
tests/unit/test_retention.py
tests/integration_cpu/test_database_migrations.py
tests/integration_cpu/test_persistent_jobs.py
tests/integration_cpu/test_dispatcher_cancel_retry.py
tests/integration_cpu/test_segment_versions.py
tests/integration_cpu/test_late_results.py
tests/integration_cpu/test_cache_integration.py
tests/integration_cpu/test_retention_recovery.py
tests/integration_cpu/test_batch2_api.py
tests/process/test_control_single_instance.py
tests/process/test_crash_recovery.py
tests/quality/test_faster_whisper_quality.py
```

### 修改

```text
pyproject.toml
uv.lock
config/open-source-reuse.yaml
config/app.fake.yaml
config/app.example.yaml
src/voice_pipeline/models/schemas.py
src/voice_pipeline/models/ports.py
src/voice_pipeline/core/config.py
src/voice_pipeline/core/jobs.py
src/voice_pipeline/core/gpu_queue.py
src/voice_pipeline/core/pipeline.py
src/voice_pipeline/api/app.py
src/voice_pipeline/api/dependencies.py
src/voice_pipeline/api/routes.py
src/voice_pipeline/cli.py
src/voice_pipeline/runtime/doctor.py
scripts/start.ps1
scripts/stop.ps1
README.md
tests/contract/test_cli_json_contract.py
tests/integration_cpu/test_api_jobs.py
tests/process/test_start_stop_scripts.py
```

`core/jobs.py` 最终只保留公共 `JobStore` Protocol/兼容 re-export；不得同时保留另一套生产内存事实源。

---

## 4. 任务依赖图

```text
Task 0 prerequisite gate
  └─ Task 1 OSS/dependencies
      ├─ Task 2 domain/state contracts
      │   └─ Task 4 persistent JobStore
      │       └─ Task 5 durable dispatcher/cancel/retry
      └─ Task 3 database/migrations/lock
          ├─ Task 4 persistent JobStore
          ├─ Task 6 immutable Artifact Store
          │   ├─ Task 7 segment versions/OCC/API
          │   └─ Task 8 versioned cache
          └─ Task 10 retention/recovery
      Task 9 quality gate uses Tasks 6 and 8
      Task 11 application/CLI/doctor integrates Tasks 3–10
      Task 12 fault/process tests exercises Tasks 3–11
      Task 13 developer verification/handoff follows Task 12
```

---

## 5. 开发任务

### Task 0: 验证批次 1 验收门并建立隔离 worktree

**Files:**
- Read: `D:\TTSsystem\runtime\handoff\batch1-developer-report.json`
- Read: `D:\TTSsystem\runtime\handoff\batch1-acceptance.json`
- Read: `D:\TTSsystem\docs\superpowers\plans\2026-08-07-batch-1-dual-engine-core.md`
- Create at execution time: `D:\TTSsystem-batch2\`

**Interfaces:**
- Consumes: 主智能体签发的 `Batch1AcceptanceReceipt`.
- Produces: 基于已验收 SHA 的 `feature/batch2-reliability` 独立 worktree。

`batch1-acceptance.json` 的冻结 schema：

```json
{
  "schema_version": 1,
  "commit_sha": "40 lowercase hexadecimal characters",
  "engineering_disposition": "PASS",
  "golden_listening": "PASS | waived_by_user",
  "waiver_reason": "required when golden_listening is waived_by_user",
  "gates": {
    "A": "PASS",
    "B": "PASS",
    "C": "PASS",
    "D": "PASS",
    "E": "PASS",
    "F": "PASS",
    "G": "PASS",
    "H": "PASS"
  },
  "evidence_root": "absolute local path",
  "signed_at_utc": "RFC3339 timestamp",
  "signed_by": "primary-acceptance-agent"
}
```

- [ ] **Step 1: 运行 prerequisite gate**

```powershell
$ErrorActionPreference = 'Stop'
$DeveloperReport = 'D:\TTSsystem\runtime\handoff\batch1-developer-report.json'
$AcceptanceReceipt = 'D:\TTSsystem\runtime\handoff\batch1-acceptance.json'

if (-not (Test-Path -LiteralPath $DeveloperReport -PathType Leaf)) {
  throw 'NOT READY: batch1 developer report is absent'
}
if (-not (Test-Path -LiteralPath $AcceptanceReceipt -PathType Leaf)) {
  throw 'NOT READY: primary-agent batch1 acceptance receipt is absent'
}

$Dev = Get-Content -LiteralPath $DeveloperReport -Raw | ConvertFrom-Json
$Acceptance = Get-Content -LiteralPath $AcceptanceReceipt -Raw | ConvertFrom-Json
if ($Acceptance.schema_version -ne 1 -or $Acceptance.engineering_disposition -ne 'PASS') {
  throw 'NOT READY: batch1 engineering disposition is not PASS'
}
if ($Acceptance.golden_listening -notin @('PASS', 'waived_by_user')) {
  throw 'NOT READY: batch1 golden listening status is invalid'
}
if ($Acceptance.golden_listening -eq 'waived_by_user' -and [string]::IsNullOrWhiteSpace($Acceptance.waiver_reason)) {
  throw 'NOT READY: batch1 golden waiver reason is absent'
}
if ($Dev.commit_sha -ne $Acceptance.commit_sha) {
  throw 'NOT READY: developer and acceptance SHA differ'
}
if (($Acceptance.gates.PSObject.Properties.Value | Where-Object { $_ -ne 'PASS' }).Count) {
  throw 'NOT READY: at least one batch1 gate is not PASS'
}

git -C D:\TTSsystem cat-file -e "$($Acceptance.commit_sha)^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'batch1 accepted commit is absent from repository' }
```

Expected: 输出为空、退出码 0。工程门缺失或非 PASS 都停止；人工黄金试听可仅以用户明确豁免的 `waived_by_user` 状态放行，且不得改写为 PASS。

- [ ] **Step 2: 创建独立 worktree**

```powershell
$Batch1Sha = $Acceptance.commit_sha
if (Test-Path -LiteralPath 'D:\TTSsystem-batch2') {
  throw 'D:\TTSsystem-batch2 already exists; inspect it instead of overwriting'
}
git -C D:\TTSsystem worktree add D:\TTSsystem-batch2 -b feature/batch2-reliability $Batch1Sha
if ($LASTEXITCODE -ne 0) { throw 'git worktree add failed' }
```

Expected: `D:\TTSsystem-batch2` 位于 `feature/batch2-reliability`，HEAD 等于已验收 SHA。

- [ ] **Step 3: 验证隔离基线**

```powershell
Set-Location D:\TTSsystem-batch2
if ((git rev-parse HEAD).Trim() -ne $Batch1Sha) { throw 'wrong worktree base' }
if (git status --short) { throw 'new worktree is dirty' }
uv sync --frozen --extra dev --python 3.11
uv run pytest tests/unit tests/contract tests/integration_cpu tests/process `
  -m 'not gpu and not gpu_residency' -q -W error
if ($LASTEXITCODE -ne 0) { throw 'accepted baseline no longer passes' }
```

Expected: 批次 1 CPU suite 全过。此 Task 不提交代码。

---

### Task 1: 锁定开源复用、依赖与机器可读审计

**Files:**
- Modify: `D:\TTSsystem-batch2\pyproject.toml`
- Modify: `D:\TTSsystem-batch2\uv.lock`
- Modify: `D:\TTSsystem-batch2\config\open-source-reuse.yaml`
- Create: `D:\TTSsystem-batch2\docs\batch-2-open-source-reuse.md`
- Create: `D:\TTSsystem-batch2\tests\contract\test_open_source_reuse.py`

**Interfaces:**
- Consumes: 批次 1 的复用清单 schema 和 locked control environment。
- Produces: 可由代码和独立验收验证的 Batch 2 reuse entries 与 frozen `uv.lock`。

- [ ] **Step 1: 写失败的 reuse contract test**

```python
from pathlib import Path

import yaml


REQUIRED = {
    "sqlalchemy": ("MIT", "reuse"),
    "alembic": ("MIT", "reuse"),
    "aiosqlite": ("MIT", "reuse"),
    "portalocker": ("BSD-3-Clause", "reuse"),
    "faster-whisper": ("MIT", "reuse"),
    "silero-vad": ("MIT", "reuse"),
    "rapidfuzz": ("MIT", "reuse"),
    "sqlmodel": (None, "rejected"),
    "diskcache": (None, "rejected"),
}


def test_batch2_reuse_inventory_is_actionable() -> None:
    payload = yaml.safe_load(Path("config/open-source-reuse.yaml").read_text("utf-8"))
    modules = {module["module_id"]: module for module in payload["modules"]}
    for module_id, (spdx, disposition) in REQUIRED.items():
        entry = modules[module_id]
        assert entry["introduced_in_batch"] == 2
        assert entry["disposition"] == disposition
        assert entry["candidates"]
        candidate = entry["candidates"][0]
        assert candidate["repository"].startswith("https://")
        assert candidate["pin"]
        assert entry["wrapper_boundary"]
        assert entry["decision_reason"]
        if spdx is not None:
            assert candidate["spdx_license"] == spdx
            assert entry["selected"]
            assert entry["lock_reference"]
        else:
            assert entry["selected"] is None
            assert entry["rejected_reasons"]
```

- [ ] **Step 2: 运行测试并确认因缺少 Batch 2 entries 失败**

Run:

```powershell
uv run pytest tests/contract/test_open_source_reuse.py -vv
```

Expected: FAIL，缺少 `sqlalchemy` 等 entry。

- [ ] **Step 3: 增加生产依赖约束**

在 `[project].dependencies` 加入：

```toml
"sqlalchemy[asyncio]>=2.0.50,<2.1",
"alembic>=1.18.5,<2",
"aiosqlite>=0.22,<0.23",
"portalocker>=4,<5",
"faster-whisper>=1.2,<2",
"RapidFuzz>=3.14,<4",
"huggingface-hub>=1.4,<2",
```

在 pytest markers 加入：

```toml
"quality_model: requires the pinned local faster-whisper model asset",
"crash_recovery: kills and restarts the local control process",
```

运行：

```powershell
uv lock
uv sync --frozen --extra dev --python 3.11
```

Expected: `uv.lock` 精确记录所有直接和传递依赖；不得手工编辑 lock。

- [ ] **Step 4: 写入复用清单和说明**

保持批次 1 的顶层 `schema_version/modules` 和 `module_id/need/disposition/candidates/selected/decision_reason/wrapper_boundary/lock_reference/rejected_reasons` 结构；新 module 增加 `introduced_in_batch: 2`。每个 candidate 都写入实际从 `uv.lock` 解析出的精确版本；adopted module 的 `lock_reference` 固定指向 `uv.lock`，模型另指向 `config/quality-model.lock.yaml`。拒绝项至少包括：

```yaml
- module_id: sqlmodel
  introduced_in_batch: 2
  need: ORM 与 Pydantic 合并建模候选
  disposition: rejected
  candidates:
    - repository: https://github.com/fastapi/sqlmodel
      spdx_license: MIT
      pin: review-2026-08-07
  selected: null
  decision_reason: Existing frozen Pydantic API models must remain separate from ORM rows.
  wrapper_boundary: none
  lock_reference: null
  rejected_reasons:
    - It couples API/domain schemas to persistence rows and still requires Alembic.
- module_id: diskcache
  introduced_in_batch: 2
  need: 本地合成缓存候选
  disposition: rejected
  candidates:
    - repository: https://github.com/grantjenks/python-diskcache
      spdx_license: Apache-2.0
      pin: review-2026-08-07
  selected: null
  decision_reason: Its separate SQLite and eviction semantics cannot atomically protect current versions and GSV parent references.
  wrapper_boundary: none
  lock_reference: null
  rejected_reasons:
    - A second cache database cannot participate in the version/current protection graph.
```

`docs/batch-2-open-source-reuse.md` 解释为什么只有 durable state machine、OCC、cache key、file/DB publication、retention protection 和验收 challenge 是项目特有薄层。

- [ ] **Step 5: 运行契约和 lock 检查**

```powershell
uv lock --check
uv run pytest tests/contract/test_open_source_reuse.py -vv
uv run python -c "import sqlalchemy, alembic, aiosqlite, portalocker, faster_whisper, rapidfuzz; print('batch2-dependencies-ok')"
```

Expected: PASS，并输出 `batch2-dependencies-ok`。

- [ ] **Step 6: 提交**

```powershell
git add pyproject.toml uv.lock config/open-source-reuse.yaml `
  docs/batch-2-open-source-reuse.md tests/contract/test_open_source_reuse.py
git commit -m "build: lock batch two storage and quality dependencies"
```

---

### Task 2: 定义持久领域模型、稳定错误和 job 状态机

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\models\persistence.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\core\state_machine.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\models\schemas.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\errors.py`
- Create: `D:\TTSsystem-batch2\tests\unit\test_persistence_models.py`
- Create: `D:\TTSsystem-batch2\tests\unit\test_job_state_machine.py`

**Interfaces:**
- Consumes: 现有 `StrictModel`, `EmotionVector`, `NonBlankText`, `LanguageCode`, `EngineFingerprint`, `AudioResult`.
- Produces: `JobStatus`, `JobKind`, `ActivationOutcome`, `PersistentJobRecord`, task/segment/version request/response schema；纯函数 `require_transition()`。

冻结类型：

```python
JobKind = Literal["reference", "gsv", "segment"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
ArtifactType = Literal["reference", "gsv"]
ArtifactState = Literal["ready", "deleting", "deleted", "missing", "corrupt"]
ActivationOutcome = Literal["not_applicable", "activated", "history_only", "cancelled"]
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
```

跨任务 record 也在 `persistence.py` 一次定义：

```python
class OutputAudioSpec(StrictModel):
    format: Literal["wav"] = "wav"
    sample_rate: int | None = Field(default=None, ge=8_000, le=192_000)
    channels: Literal[1] = 1
    sample_width_bits: Literal[16] = 16


class SegmentJobSnapshot(StrictModel):
    task_id: UUID
    segment_id: UUID
    ref_draft_revision: int
    gsv_draft_revision: int
    selection_revision: int
    active_ref_version_id: UUID | None
    active_gsv_version_id: UUID | None
    activate_on_success: bool


class JobSuccessCommit(StrictModel):
    result: dict[str, JsonValue]
    activation_outcome: ActivationOutcome = "not_applicable"
    artifact_version_ids: tuple[UUID, ...] = ()
    reference_cache_hit: bool = False
    gsv_cache_hit: bool = False


class CancelDecision(StrictModel):
    action: Literal[
        "queued_cancelled",
        "running_cancel_requested",
        "already_cancelled",
        "terminal_conflict",
    ]
    record: "PersistentJobRecord"


class RecoverySummary(StrictModel):
    interrupted_job_ids: tuple[UUID, ...]
    queued_job_ids: tuple[UUID, ...]


class DispatcherStats(StrictModel):
    state: Literal["stopped", "running", "stopping"]
    queued_count: int
    active_job_id: UUID | None
    recovered_interrupted_count: int
```

`PersistentJobRecord` 和 task/segment/version record 的字段逐一对应第 2.1 节表；API schema 不 import SQLAlchemy ORM class。
`sample_rate=None` 表示保留该 engine 的 pinned native rate；task 的 final GSV output spec 可显式设为 32000。cache key 使用实际传给对应 engine 的有效 spec，不能把 final spec 错套到 Index reference。

- [ ] **Step 1: 写状态机失败测试**

```python
import pytest

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.core.state_machine import require_transition


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "interrupted"),
    ],
)
def test_allows_only_frozen_job_transitions(before: str, after: str) -> None:
    require_transition(before, after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("succeeded", "running"),
        ("failed", "queued"),
        ("cancelled", "running"),
        ("interrupted", "running"),
        ("queued", "succeeded"),
    ],
)
def test_rejects_illegal_or_reopened_job_transitions(before: str, after: str) -> None:
    with pytest.raises(PipelineError, match="JOB_STATE_CONFLICT"):
        require_transition(before, after)
```

- [ ] **Step 2: 写 schema 失败测试**

测试必须覆盖：

```python
def test_retry_mode_is_frozen_snapshot_only() -> None:
    assert RetryJobRequest(mode="frozen_snapshot").mode == "frozen_snapshot"
    with pytest.raises(ValidationError):
        RetryJobRequest(mode="current")


def test_segment_patch_requires_at_least_one_change() -> None:
    with pytest.raises(ValidationError):
        SegmentInputsPatch(
            expected_ref_draft_revision=1,
            expected_gsv_draft_revision=1,
        )


def test_gsv_version_requires_reference_binding() -> None:
    with pytest.raises(ValidationError):
        ArtifactVersionRecord(
            artifact_type="gsv",
            ref_version_id=None,
            ref_content_sha256=None,
            **valid_artifact_fields(),
        )
```

还须测试：

- source range 非负、`source_start < source_end`；
- `source_text` 必须等于 task 原文切片的检查由 service 完成；
- vector 复用共享 validator；
- UUID、SHA-256、UTC timestamp；
- `active_ref_version_id` 和 `active_gsv_version_id` 可空；
- `request_id` 不唯一；
- frozen model 拒绝 extra。

- [ ] **Step 3: 运行测试确认缺少模型和状态机**

```powershell
uv run pytest tests/unit/test_job_state_machine.py tests/unit/test_persistence_models.py -vv
```

Expected: collection/import FAIL。

- [ ] **Step 4: 实现状态机和错误码**

`state_machine.py` 的完整逻辑：

```python
from voice_pipeline.core.errors import ErrorCode, PipelineError
from voice_pipeline.models.persistence import JobStatus

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "interrupted"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
}


def require_transition(before: JobStatus, after: JobStatus) -> None:
    if after not in _ALLOWED[before]:
        raise PipelineError(
            ErrorCode.JOB_STATE_CONFLICT,
            "job_state",
            f"illegal job transition: {before} -> {after}",
            retryable=False,
            details={"before": before, "after": after},
        )
```

新增稳定错误码：

```text
JOB_STATE_CONFLICT
JOB_NOT_RETRYABLE
DATABASE_BUSY
DATABASE_MIGRATION_REQUIRED
DATABASE_INTEGRITY_FAILED
CONTROL_INSTANCE_CONFLICT
ARTIFACT_MISSING
ARTIFACT_CORRUPT
CACHE_KEY_COLLISION
VERSION_CONFLICT
VERSION_NOT_READY
QUALITY_VAD_FAILED
QUALITY_TEXT_MISMATCH
QUALITY_MODEL_UNAVAILABLE
RETENTION_PLAN_STALE
```

- [ ] **Step 5: 实现 Pydantic schema**

`persistence.py` 至少导出：

```python
class RetryJobRequest(StrictModel):
    mode: Literal["frozen_snapshot"] = "frozen_snapshot"


class SegmentReferenceJobRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    activate_on_success: bool = True


class SegmentGsvJobRequest(StrictModel):
    request_id: UUID
    activate_on_success: bool = True


class ActivateVersionRequest(StrictModel):
    expected_selection_revision: int = Field(ge=0)


class SegmentInputsPatch(StrictModel):
    expected_ref_draft_revision: int = Field(ge=0)
    expected_gsv_draft_revision: int = Field(ge=0)
    ref_text_cn: NonBlankText | None = None
    current_emotion_vector: EmotionVector | None = None
    synthesis_text: NonBlankText | None = None
    speed_factor: float | None = Field(default=None, ge=0.5, le=2.0)
    pause_after_ms: int | None = Field(default=None, ge=0, le=30_000)
    seed: int | None = None

    @model_validator(mode="after")
    def require_change(self) -> "SegmentInputsPatch":
        changed = (
            self.ref_text_cn,
            self.current_emotion_vector,
            self.synthesis_text,
            self.speed_factor,
            self.pause_after_ms,
            self.seed,
        )
        if all(value is None for value in changed):
            raise ValueError("at least one segment input must change")
        return self
```

定义的 record 必须保留 timezone-aware UTC datetime，不接受 NaN/Infinity JSON。

- [ ] **Step 6: 运行测试和类型检查**

```powershell
uv run pytest tests/unit/test_job_state_machine.py tests/unit/test_persistence_models.py -vv
uv run mypy src/voice_pipeline/models src/voice_pipeline/core/state_machine.py
uv run ruff check src/voice_pipeline/models src/voice_pipeline/core/state_machine.py tests/unit
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/voice_pipeline/models/persistence.py src/voice_pipeline/models/schemas.py `
  src/voice_pipeline/core/state_machine.py src/voice_pipeline/core/errors.py `
  tests/unit/test_persistence_models.py tests/unit/test_job_state_machine.py
git commit -m "feat: define durable job and artifact contracts"
```

---

### Task 3: 建立 SQLite engine、Alembic migration 和单实例锁

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\__init__.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\database.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\orm.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\migrations\__init__.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\migrations\env.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\migrations\script.py.mako`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\migrations\versions\0001_batch2_foundation.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\config.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_database_migrations.py`
- Create: `D:\TTSsystem-batch2\tests\process\test_control_single_instance.py`

**Interfaces:**
- Consumes: Task 2 domain enums/schema。
- Produces: `Database.open()`, `Database.close()`, `Database.read_session()`, `Database.write_session()`, `ControlInstanceLock`, packaged Alembic head。

冻结接口：

```python
class Database:
    @classmethod
    async def open(
        cls,
        settings: StorageSettings,
        *,
        instance_id: UUID,
        migrate: bool,
    ) -> "Database": ...

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]: ...

    @asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]: ...

    async def quick_check(self) -> None: ...
    async def close(self) -> None: ...
```

- [ ] **Step 1: 写 empty upgrade、约束和 restart 测试**

测试以文件数据库而非 `:memory:` 运行，断言：

```python
async def test_empty_database_upgrades_to_packaged_head(tmp_path: Path) -> None:
    settings = storage_settings(tmp_path)
    database = await Database.open(settings, instance_id=uuid4(), migrate=True)
    try:
        assert await database.scalar_text("PRAGMA journal_mode") == "wal"
        assert await database.scalar_int("PRAGMA foreign_keys") == 1
        assert await database.alembic_revision() == PACKAGED_HEAD
        assert await database.quick_check_text() == "ok"
    finally:
        await database.close()


async def test_request_id_is_not_unique(database: Database) -> None:
    request_id = uuid4()
    first = await insert_queued_job(database, request_id=request_id)
    second = await insert_queued_job(database, request_id=request_id)
    assert first.job_id != second.job_id
```

迁移测试还须：

- empty→head；
- head→base→head 在数据库副本上；
- `PRAGMA foreign_key_check` 零行；
- immutable version UPDATE trigger 拒绝 payload 修改；
- current activation trigger 拒绝跨 segment/type；
- migration 文件从 wheel 的 package resources 可发现。

- [ ] **Step 2: 写单实例进程测试**

第一个 helper 进程持有 `runtime/state/control.lock` 后，第二个 helper 必须在 2 秒内以稳定 `CONTROL_INSTANCE_CONFLICT` 非零退出；杀死第一个后，第三个可以获得 OS lock。测试按 PID/create-time 清理，不能仅按 PID。

- [ ] **Step 3: 运行测试确认数据库层不存在**

```powershell
uv run pytest tests/integration_cpu/test_database_migrations.py `
  tests/process/test_control_single_instance.py -vv
```

Expected: import/collection FAIL。

- [ ] **Step 4: 实现 StorageSettings 和本地路径门**

```python
class StorageSettings(StrictModel):
    database_path: Path
    artifact_root: Path
    control_lock_path: Path
    busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    wal_autocheckpoint_pages: int = Field(default=1000, ge=1)
    history_limit: Literal[5] = 5
    cache_max_entries_per_kind: int = Field(default=500, ge=10)
    cache_max_age_days: int = Field(default=90, ge=1)
```

配置解析后要求三条路径为绝对、本地非 UNC，且位于 `runtime_dir`。

- [ ] **Step 5: 实现 engine pragmas 和 write serialization**

`database.py` 的 connect hook 对每个连接执行：

```python
cursor.execute("PRAGMA foreign_keys=ON")
cursor.execute("PRAGMA synchronous=FULL")
cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
cursor.execute(f"PRAGMA wal_autocheckpoint={wal_autocheckpoint_pages}")
```

`Database.open()` 在单实例锁内用首连接执行并验证 `journal_mode=WAL`。每连接 hook 设置 `foreign_keys`、`synchronous`、`busy_timeout` 和 `wal_autocheckpoint`，不要在每次 checkout 重复切换 journal mode。`write_session()` 获取 `asyncio.Lock`，开启短 transaction，捕获 `sqlite3.OperationalError` 中的 busy/locked 并转换为 retryable `DATABASE_BUSY`；不得把网络/模型调用放在锁内。

- [ ] **Step 6: 实现 packaged Alembic migration**

`0001_batch2_foundation.py` 创建第 2 节全部表、CHECK、FK、unique index 和 trigger。SQLite 表变更配置 `render_as_batch=True`，所有 constraint 显式命名。migration 前如数据库非空，使用 stdlib `sqlite3.Connection.backup()` 写入：

```text
runtime/backups/pipeline-before-{from-revision}-{utc-timestamp}.sqlite3
```

应用启动只能在持有 `ControlInstanceLock` 后升级。

- [ ] **Step 7: 实现 portalocker 单实例**

lock 文件 payload 使用批次 1 的原子可变状态写法，包含：

```json
{
  "schema_version": 1,
  "instance_id": "uuid",
  "pid": 1234,
  "create_time": 1780000000.0,
  "database_path": "absolute local path"
}
```

OS lock 是权威。`control.lock` 本身只由 portalocker 持有，不得在持锁期间用 `os.replace()` 换掉 inode/file identity；owner payload 写入独立的 `control-lock-owner.json` 并原子替换。正常/异常上下文退出都释放 lock 句柄，再删除只属于同一 instance/PID/create-time 的 owner payload。

- [ ] **Step 8: 运行数据库测试、wheel resource test 和 lint**

```powershell
uv run pytest tests/integration_cpu/test_database_migrations.py `
  tests/process/test_control_single_instance.py -vv -W error
uv run mypy src/voice_pipeline/storage src/voice_pipeline/core/config.py
uv run ruff check src/voice_pipeline/storage tests/integration_cpu/test_database_migrations.py `
  tests/process/test_control_single_instance.py
uv build --wheel --out-dir runtime/dist-task3
```

Expected: 全部 PASS，wheel 中包含 migrations。

- [ ] **Step 9: 提交**

```powershell
git add src/voice_pipeline/storage src/voice_pipeline/core/config.py `
  tests/integration_cpu/test_database_migrations.py `
  tests/process/test_control_single_instance.py
git commit -m "feat: add migrated sqlite storage foundation"
```

---

### Task 4: 用 PersistentJobStore 替换内存 registry

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\job_store.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\jobs.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\runtime\state_audit.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_persistent_jobs.py`
- Modify: `D:\TTSsystem-batch2\tests\unit\test_jobs.py`

**Interfaces:**
- Consumes: `Database`, `PersistentJobRecord`, `require_transition()`。
- Produces: `JobStore` Protocol、`SqliteJobStore`、`RecoverySummary`、`StateAuditWriter`。

冻结 Protocol：

```python
class JobStore(Protocol):
    async def create(
        self,
        *,
        request_id: UUID,
        kind: JobKind,
        request_snapshot: dict[str, JsonValue],
        segment_snapshot: SegmentJobSnapshot | None = None,
        retry_of_job_id: UUID | None = None,
        attempt: int = 1,
    ) -> ExecutionContext: ...

    async def get(self, job_id: UUID) -> PersistentJobRecord: ...
    async def list_queued(self, *, limit: int) -> list[PersistentJobRecord]: ...
    async def claim(self, job_id: UUID, *, instance_id: UUID) -> bool: ...
    async def request_cancel(self, job_id: UUID) -> CancelDecision: ...
    async def mark_succeeded(self, job_id: UUID, result: JobSuccessCommit) -> bool: ...
    async def mark_failed(self, job_id: UUID, error: dict[str, JsonValue]) -> bool: ...
    async def mark_cancelled(self, job_id: UUID, error: dict[str, JsonValue]) -> bool: ...
    async def interrupt_previous_instance(self, *, instance_id: UUID) -> RecoverySummary: ...
    async def clone_for_retry(self, job_id: UUID) -> ExecutionContext: ...
```

- [ ] **Step 1: 写 persistence/recovery/immutability 失败测试**

核心用例：

```python
async def test_job_survives_database_reopen(tmp_path: Path) -> None:
    first_db, first_store = await open_store(tmp_path)
    context = await first_store.create(
        request_id=uuid4(),
        kind="reference",
        request_snapshot=valid_reference_snapshot(tmp_path),
    )
    await first_db.close()

    second_db, second_store = await open_store(tmp_path)
    try:
        record = await second_store.get(context.job_id)
        assert record.status == "queued"
        assert record.request_snapshot_sha256 == canonical_sha(record.request_snapshot)
    finally:
        await second_db.close()


async def test_startup_interrupts_only_foreign_running_jobs(store: SqliteJobStore) -> None:
    old = await create_and_claim(store, instance_id=uuid4())
    current_instance = uuid4()
    summary = await store.interrupt_previous_instance(instance_id=current_instance)
    assert summary.interrupted_job_ids == [old.job_id]
    assert (await store.get(old.job_id)).status == "interrupted"
```

还须覆盖：

- 同 request ID 两个 job；
- created_at/job_id FIFO；
- claim 只有一个 winner；
- terminal 无法改写；
- result/error mutual exclusion；
- retry 新 ID、同 request、attempt+1、snapshot hash 相同；
- interrupted/failed/cancelled 可 retry，queued/running/succeeded 不可；
- DB busy 转换稳定错误，不暴露 traceback。

- [ ] **Step 2: 运行测试确认当前 registry 不持久**

```powershell
uv run pytest tests/integration_cpu/test_persistent_jobs.py tests/unit/test_jobs.py -vv
```

Expected: 新 persistence tests FAIL。

- [ ] **Step 3: 实现 JobStore 和 SQLite CAS**

关键 claim 必须是一条有条件 UPDATE：

```sql
UPDATE generation_jobs
SET status = 'running',
    stage = 'running',
    runner_instance_id = :instance_id,
    started_at_utc = :now
WHERE job_id = :job_id
  AND status = 'queued'
  AND cancel_requested_at_utc IS NULL
```

以 `rowcount == 1` 判断 claim。所有终态写入同样带 `WHERE status='running'` 和取消条件，不先 SELECT 再无条件 UPDATE。

`request_snapshot_json` 由共享 canonical JSON 函数生成，同时写 SHA。retry 直接解析原 JSON，不从当前 segment 或文件重新构造。

`error_json` 始终保存批次 1 稳定 envelope 的 `code/stage/message/retryable/details`；`attempt` 是重试次数。原生 SQL 文本和 traceback 只进入受控本地诊断日志，不进入公共 message。

- [ ] **Step 4: 实现 state audit**

`state-audit.jsonl` 每行固定：

```text
schema_version
timestamp_utc
instance_id
event
job_id
request_id
task_id
segment_id
version_id
artifact_sha256
details
```

事件 enum：

```text
job_created
job_claimed
job_terminal
job_recovered
artifact_staged
quality_passed
artifact_published
version_committed
current_cas_applied
current_cas_skipped
cleanup_candidate
cleanup_file_removed
cache_hit
cache_invalidated
```

append 在控制面单进程锁内执行并 flush；需要崩溃窗口证据的事件再调用 `os.fsync`。

- [ ] **Step 5: 保留 core.jobs 兼容边界**

`core/jobs.py` re-export `JobStore`, `JobKind`, `JobStatus`, `PersistentJobRecord`；删除生产 `InMemoryJobRegistry` 构造路径。可保留测试 fake，但必须名为 `FakeJobStore` 并位于 `tests/fixtures`，不得被生产 dependency builder 导入。

- [ ] **Step 6: 运行局部和 Batch 1 job 回归**

```powershell
uv run pytest tests/integration_cpu/test_persistent_jobs.py tests/unit/test_jobs.py `
  tests/integration_cpu/test_api_jobs.py -vv -W error
uv run mypy src/voice_pipeline/storage/job_store.py src/voice_pipeline/core/jobs.py `
  src/voice_pipeline/runtime/state_audit.py
uv run ruff check src/voice_pipeline/storage/job_store.py src/voice_pipeline/core/jobs.py `
  src/voice_pipeline/runtime/state_audit.py tests/integration_cpu/test_persistent_jobs.py
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交**

```powershell
git add src/voice_pipeline/storage/job_store.py src/voice_pipeline/core/jobs.py `
  src/voice_pipeline/runtime/state_audit.py tests/integration_cpu/test_persistent_jobs.py `
  tests/unit/test_jobs.py tests/integration_cpu/test_api_jobs.py
git commit -m "feat: persist generation jobs in sqlite"
```

---

### Task 5: 引入 durable dispatcher、queued/running 取消和 frozen retry

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\core\dispatcher.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\core\job_executor.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\gpu_queue.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\api\routes.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_dispatcher_cancel_retry.py`
- Modify: `D:\TTSsystem-batch2\tests\unit\test_gpu_queue.py`
- Modify: `D:\TTSsystem-batch2\tests\integration_cpu\test_api_failures.py`

**Interfaces:**
- Consumes: `JobStore`, `SerialGpuQueue`, `SynthesisService`, `EngineRuntime`。
- Produces: `DurableJobDispatcher`, `JobExecutor`, cancel/retry API。

冻结 dispatcher 接口：

```python
class DurableJobDispatcher:
    async def start(self) -> None: ...
    async def notify(self) -> None: ...
    async def cancel(self, job_id: UUID) -> PersistentJobRecord: ...
    async def stop(self, *, deadline: float) -> None: ...
    def stats(self) -> DispatcherStats: ...
```

`JobExecutor.execute(record)` 必须只从 `record.request_snapshot` 重建对应 Pydantic request，使用由 `job_id/request_id/runtime/jobs/{job_id}` 构造的 `ExecutionContext`，不得查询“当前请求参数”替代 snapshot。

- [ ] **Step 1: 写 queued cancel 失败测试**

```python
async def test_cancelled_queued_job_never_calls_engine(app_harness: AppHarness) -> None:
    blocker = await app_harness.submit_blocking_reference()
    queued = await app_harness.submit_reference()
    response = await app_harness.client.post(f"/api/v1/jobs/{queued}/cancel")
    assert response.status_code == 202

    await app_harness.release(blocker)
    record = await app_harness.wait_terminal(queued)
    assert record["status"] == "cancelled"
    assert app_harness.index.calls_for_job(queued) == 0
```

- [ ] **Step 2: 写 running cancel、race 和 retry 失败测试**

至少包含：

```python
async def test_running_cancel_aborts_before_terminal(app_harness: AppHarness) -> None:
    job_id = await app_harness.submit_blocking_reference()
    await app_harness.wait_running(job_id)
    response = await app_harness.client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 202
    record = await app_harness.wait_terminal(job_id)
    assert record["status"] == "cancelled"
    assert app_harness.runtime.active_inference == 0
    assert app_harness.queue.stats().state == "running"


async def test_retry_creates_new_job_from_original_snapshot(app_harness: AppHarness) -> None:
    failed_id = await app_harness.submit_failing_reference()
    original = await app_harness.wait_terminal(failed_id)
    retry = await app_harness.client.post(
        f"/api/v1/jobs/{failed_id}/retry",
        json={"mode": "frozen_snapshot"},
    )
    assert retry.status_code == 202
    retry_id = retry.json()["job_id"]
    assert retry_id != failed_id
    replay = await app_harness.get_job(retry_id)
    assert replay["request_id"] == original["request_id"]
    assert replay["retry_of_job_id"] == failed_id
    assert replay["attempt"] == original["attempt"] + 1
    assert replay["request_snapshot"] == original["request_snapshot"]
```

竞态测试启动 cancel 和 fake-engine release 两个 task 过 barrier，断言最终只有 succeeded 或 cancelled，一个终态，且之后状态不再变化。

- [ ] **Step 3: 运行测试确认旧 routes 的 fire-and-forget scheduler 失败**

```powershell
uv run pytest tests/integration_cpu/test_dispatcher_cancel_retry.py `
  tests/unit/test_gpu_queue.py tests/integration_cpu/test_api_failures.py -vv
```

Expected: 新用例 FAIL。

- [ ] **Step 4: 实现 SQLite 驱动的 claim loop**

dispatcher 只有一个 loop：

```python
async def _run(self) -> None:
    while not self._stopping:
        self._wake.clear()
        records = await self._store.list_queued(limit=32)
        if not records:
            await self._wake.wait()
            continue
        for record in records:
            if self._stopping:
                return
            if not await self._store.claim(record.job_id, instance_id=self._instance_id):
                continue
            task = asyncio.create_task(
                self._execute_claimed(record.job_id),
                name=f"persistent-job-{record.job_id}",
            )
            self._active[record.job_id] = task
            try:
                await task
            finally:
                self._active.pop(record.job_id, None)
```

`_execute_claimed()` 调用现有 `queue.run()`，保持 engine 并发 1。不得为每个 HTTP submission 建立未跟踪 scheduler task。
event 必须在查 DB 前 clear：submission 若发生在 clear 之后会 set event；若发生在 clear 之前，queued row 会被本次查询看到，避免 lost wakeup。

- [ ] **Step 5: 让 queue 跳过已取消 waiter**

若等待 `queue.run()` 的 future 已 cancelled，consumer 在执行 factory 前跳过；若 factory 已开始，则 cancellation 传播到 service，由批次 1 `_invoke_engine` 的 CancelledError/abort 流程处理。新增 stats：

```text
cancelled_before_start
cancelled_while_running
```

- [ ] **Step 6: 实现 cancel/retry 路由**

route 只调用 dispatcher/store：

```python
@router.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: UUID) -> JSONResponse:
    record = await plane.dispatcher.cancel(job_id)
    status_code = 200 if record.status == "cancelled" else 202
    return JSONResponse(
        status_code=status_code,
        content={
            "job_id": str(record.job_id),
            "status": record.status,
            "cancellation_requested": record.cancel_requested_at is not None,
        },
    )


@router.post("/api/v1/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: UUID, request: RetryJobRequest) -> dict[str, JsonValue]:
    context = await plane.registry.clone_for_retry(job_id)
    await plane.dispatcher.notify()
    return await build_submission_envelope(plane.registry, context.job_id)
```

unknown job 为 404；illegal terminal 为 409。

- [ ] **Step 7: 实现 shutdown/recovery 协调**

正常 stop：

1. 停止接单；
2. dispatcher 停止 claim；
3. active task 在共享 deadline 内取消并 abort；
4. 确认后写 interrupted，queued 留在 DB；
5. queue/runtime 使用同一 absolute deadline；
6. DB close 和 lock release 最后执行。

硬重启：

1. runtime 清理前实例 worker；
2. DB `running -> interrupted`；
3. recovery/reconcile；
4. dispatcher 重投 queued；
5. 才开始 HTTP。

- [ ] **Step 8: 运行取消、重试和 Batch 1 互斥回归**

```powershell
uv run pytest tests/integration_cpu/test_dispatcher_cancel_retry.py `
  tests/unit/test_gpu_queue.py tests/integration_cpu/test_api_failures.py `
  tests/integration_cpu/test_api_jobs.py -vv -W error
uv run mypy src/voice_pipeline/core/dispatcher.py `
  src/voice_pipeline/core/job_executor.py src/voice_pipeline/api/routes.py
uv run ruff check src/voice_pipeline/core src/voice_pipeline/api `
  tests/integration_cpu/test_dispatcher_cancel_retry.py
```

Expected: 全部 PASS，fake engine `max_active_observed == 1`。

- [ ] **Step 9: 提交**

```powershell
git add src/voice_pipeline/core/dispatcher.py src/voice_pipeline/core/job_executor.py `
  src/voice_pipeline/core/gpu_queue.py src/voice_pipeline/api/routes.py `
  tests/integration_cpu/test_dispatcher_cancel_retry.py tests/unit/test_gpu_queue.py `
  tests/integration_cpu/test_api_failures.py tests/integration_cpu/test_api_jobs.py
git commit -m "feat: add durable dispatch cancellation and retry"
```

---

### Task 6: 建立内容寻址不可变 Artifact Store 和启动协调器

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\artifact_store.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\recovery.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\modules\audio\atomic_output.py`
- Create: `D:\TTSsystem-batch2\tests\unit\test_artifact_store.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_artifact_recovery.py`

**Interfaces:**
- Consumes: 批次 1 `reserve_output_path()`, `probe_wav()`, `sha256_file()`。
- Produces: `ArtifactStore.stage_audio()`, `publish_blob()`, `materialize_job_output()`, `publish_version_manifest()`, `StorageRecovery.reconcile()`。

冻结类型：

```python
class StagedArtifact(StrictModel):
    path: Path
    job_id: UUID
    audio: AudioResult
    byte_size: int


class PublishedBlob(StrictModel):
    content_sha256: str
    relative_path: Path
    absolute_path: Path
    byte_size: int
    audio: AudioResult
    reused_existing: bool


class RecoveryReport(StrictModel):
    recovery_run_id: UUID
    removed_partials: tuple[Path, ...]
    quarantined_orphans: tuple[Path, ...]
    missing_versions: tuple[UUID, ...]
    corrupt_versions: tuple[UUID, ...]
```

- [ ] **Step 1: 写不可变发布和路径安全失败测试**

用例必须覆盖：

```python
def test_same_audio_reuses_content_addressed_blob(store: ArtifactStore, tone: Path) -> None:
    first = store.publish_blob(store.stage_audio(uuid4(), tone))
    second = store.publish_blob(store.stage_audio(uuid4(), tone))
    assert first.absolute_path == second.absolute_path
    assert second.reused_existing is True
    assert sha256_file(first.absolute_path) == first.content_sha256


def test_existing_wrong_content_is_never_overwritten(store: ArtifactStore, tone: Path) -> None:
    expected_sha = sha256_file(tone)
    destination = store.blob_path(expected_sha)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"sentinel")
    sentinel_sha = sha256_file(destination)
    with pytest.raises(PipelineError, match="ARTIFACT_CORRUPT"):
        store.publish_blob(store.stage_audio(uuid4(), tone))
    assert sha256_file(destination) == sentinel_sha
```

还须覆盖：

- relative/UNC/越界/symlink 拒绝；
- partial 失败只删除本次 owned file；
- cache materialize 生成新 job path 且不覆盖；
- materialize 始终复制，job 路径的后续写入不能反向修改 canonical blob；
- published blob 外部篡改被探测；
- manifest O_EXCL；
- Windows Unicode/空格路径。

- [ ] **Step 2: 写四个崩溃窗口协调测试**

通过预造 filesystem/DB 状态模拟：

1. partial 无 DB；
2. published blob 无 DB；
3. ready DB row + valid blob；
4. ready DB row + missing/corrupt blob。

期望：

- partial 删除；
- 超过 grace 的 orphan 移入 quarantine；
- valid 不改；
- missing/corrupt version state 降级，current 不自动回退。

- [ ] **Step 3: 运行测试确认模块缺失**

```powershell
uv run pytest tests/unit/test_artifact_store.py `
  tests/integration_cpu/test_artifact_recovery.py -vv
```

Expected: import/collection FAIL。

- [ ] **Step 4: 实现内容寻址路径和发布**

blob path 只能由小写 SHA 派生：

```python
def blob_path(self, content_sha256: str) -> Path:
    require_sha256(content_sha256)
    return self._root / "blobs" / "sha256" / content_sha256[:2] / f"{content_sha256}.wav"
```

`stage_audio()` 复制到同 volume staging，fsync，调用 `probe_wav` 并核对源/阶段 SHA。`publish_blob()` 对不存在目标使用现有 OutputReservation；存在目标只读验证，绝不 replace。

- [ ] **Step 5: 实现 cache materialize**

`materialize_job_output(blob, destination)`：

1. 验证 blob path 和 SHA；
2. O_EXCL reserve destination；
3. 从 blob 复制到 owned partial、flush、fsync；
4. job output 与 canonical blob 必须是不同 file identity，不允许 hardlink/reflink；
5. 验证 partial SHA；
6. 原子发布到 reserved destination；
7. 返回针对 job path 的 `AudioResult`。

API 继续返回 `runtime/jobs/{new_job_id}` 文件。即使外部程序改写该 job copy，canonical blob 仍保持原 SHA；后续 job 下载自身文件时仍会做 Batch1 校验，version 下载始终读取 canonical blob。

- [ ] **Step 6: 实现 startup reconciliation**

协调器在 dispatcher/HTTP 前运行。对 ready version 逐个验证 path/size/SHA；发现 missing/corrupt 只更新 state、health degraded、写 audit，不静默选择旧版。quarantine receipt 包含原路径、目标路径、SHA、reason、recovery_run_id。

- [ ] **Step 7: 运行测试和原子输出回归**

```powershell
uv run pytest tests/unit/test_artifact_store.py `
  tests/integration_cpu/test_artifact_recovery.py `
  tests/unit/test_atomic_output.py -vv -W error
uv run mypy src/voice_pipeline/storage/artifact_store.py `
  src/voice_pipeline/storage/recovery.py
uv run ruff check src/voice_pipeline/storage/artifact_store.py `
  src/voice_pipeline/storage/recovery.py tests/unit/test_artifact_store.py `
  tests/integration_cpu/test_artifact_recovery.py
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```powershell
git add src/voice_pipeline/storage/artifact_store.py `
  src/voice_pipeline/storage/recovery.py `
  src/voice_pipeline/modules/audio/atomic_output.py `
  tests/unit/test_artifact_store.py `
  tests/integration_cpu/test_artifact_recovery.py `
  tests/unit/test_atomic_output.py
git commit -m "feat: publish immutable content addressed audio"
```

---

### Task 7: 建立 task/segment/version repository、OCC 和最小黑盒 API

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\segment_store.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\version_store.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\api\foundation_routes.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\job_executor.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\pipeline.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_segment_versions.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_late_results.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_batch2_api.py`

**Interfaces:**
- Consumes: `Database`, `ArtifactStore`, `JobStore`, existing Index/GSV service。
- Produces: task/segment CRUD、`VersionCommitService`、version list/activate/audio API、segment-bound reference/GSV jobs。

冻结提交接口：

```python
class VersionCommitService:
    async def commit_reference(
        self,
        *,
        job: PersistentJobRecord,
        blob: PublishedBlob,
        reference: ReferenceBinding,
        quality: QualityReport,
        activate_on_success: bool,
    ) -> VersionCommitResult: ...

    async def commit_gsv(
        self,
        *,
        job: PersistentJobRecord,
        blob: PublishedBlob,
        reference_version: ArtifactVersionRecord,
        target: AudioResult,
        activate_on_success: bool,
    ) -> VersionCommitResult: ...
```

version payload insert、job_artifacts、pointer CAS 和 job succeeded 必须在同一 write transaction。

- [ ] **Step 1: 写 task/segment CRUD 失败测试**

测试：

- create task 保存 source SHA；
- create segment 要求 `source_text == task.source_text[start:end]`；
- task 内 ordinal unique；
- patch 正确增加 ref/gsv/general revisions；
- stale patch HTTP 409；
- 重启后字段完全相同。

示例：

```python
async def test_segment_source_must_match_authoritative_slice(client: AsyncClient) -> None:
    task = await create_task(client, source_text="前半句。后半句。")
    response = await client.post(
        f"/api/v1/tasks/{task}/segments",
        json=segment_payload(
            source_start=0,
            source_end=4,
            source_text="被改写的文本",
        ),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
```

- [ ] **Step 2: 写 immutable version/current/OCC 失败测试**

至少覆盖：

```python
async def test_late_reference_is_history_only(version_harness: VersionHarness) -> None:
    job = await version_harness.start_blocking_reference()
    await version_harness.activate_other_reference()
    result = await version_harness.finish(job)
    assert result.status == "succeeded"
    assert result.activation_outcome == "history_only"
    assert result.reference_version_id in await version_harness.reference_history()
    assert await version_harness.active_reference() != result.reference_version_id


async def test_gsv_permanently_records_actual_reference(version_harness: VersionHarness) -> None:
    ref_a = await version_harness.active_reference()
    job = await version_harness.start_blocking_gsv()
    ref_b = await version_harness.create_and_activate_reference()
    gsv = await version_harness.finish(job)
    assert gsv.ref_version_id == ref_a
    assert gsv.ref_content_sha256 == await version_harness.sha(ref_a)
    assert ref_b != ref_a
```

还须覆盖：

- failed quality/engine 不创建 version/current 不动；
- cross segment/type activation 拒绝；
- activation of missing/corrupt/deleting 拒绝；
- listening/version GET 不改 current；
- GSV 无 active reference 时 409；
- reference job 不调用 GSV；
- GSV job 不调用 Index，reference blob/version/hash 不变；
- reference success/GSV 旧 current 仍可下载；
- 同一 segment 的完全相同 complete snapshot 可让不同 job 关联同一 immutable version；
- 不同 segment 即使复用同一 blob，也必须各自创建 version metadata；
- version payload UPDATE trigger 生效。

- [ ] **Step 3: 运行测试确认缺少 repository/routes**

```powershell
uv run pytest tests/integration_cpu/test_segment_versions.py `
  tests/integration_cpu/test_late_results.py `
  tests/integration_cpu/test_batch2_api.py -vv
```

Expected: FAIL。

- [ ] **Step 4: 实现 task/segment repository**

所有 patch 使用：

```sql
UPDATE segments
SET ...,
    ref_draft_revision = ref_draft_revision + :ref_delta,
    gsv_draft_revision = gsv_draft_revision + :gsv_delta,
    revision = revision + 1,
    updated_at_utc = :now
WHERE segment_id = :segment_id
  AND ref_draft_revision = :expected_ref
  AND gsv_draft_revision = :expected_gsv
```

`rowcount=0` 返回 `VERSION_CONFLICT`。创建 segment 时后端用 task 原文切片比较，不信任请求的 `source_text`。
`SegmentCreateRequest` 只接受 `llm_emotion_vector`，不接受另一套初始 current；插入时后端把同一已校验 tuple 原样复制到 `current_emotion_vector`。这冻结“默认向量就是 LLM 值，用户只在其上微调”的用户决策。

- [ ] **Step 5: 实现 segment job snapshot**

reference submission 冻结：

```text
base_voice absolute path + SHA
ref_text_cn
current_emotion_vector
seed
ref_draft_revision
active_ref_version_id
selection_revision
Index fingerprint
output spec
activate_on_success
```

GSV submission 冻结：

```text
active reference version ID/blob SHA/ref_text_cn
synthesis_text
target_language
speed_factor
seed
gsv_draft_revision
active_ref_version_id
active_gsv_version_id
selection_revision
GSV fingerprint
output spec
activate_on_success
```

snapshot 在插入 queued job 的同一 DB transaction 中读取；不能先读 segment、稍后另行插 job。

- [ ] **Step 6: 实现 version commit 与 current CAS**

为同一 segment/type 分配 display ordinal 时在 write lock 内取 `max + 1`。插入 immutable metadata/state/job_artifact 后执行第 1.5 节条件 UPDATE。结果：

```python
class VersionCommitResult(StrictModel):
    version: ArtifactVersionRecord
    activation_outcome: Literal["activated", "history_only", "cancelled"]
    cache_hit: bool
```

无论 CAS 是否成功，version 都存在；CAS 失败从未先覆盖 current。若 `cancel_requested_at` 已先提交但完整 artifact 已发布，transaction 创建 history version/job_artifact、跳过 current，并把 job 终态写为 cancelled、`activation_outcome=cancelled`；若没有完整 artifact，则只写 cancelled。

- [ ] **Step 7: 实现低层 API**

所有 API 使用 `api/v1`；版本列表默认只返回 ready，按 `created_at, version_id` 降序；version audio 仅从 DB 相对路径解析，发送前重验文件和 SHA。激活成功增加 `selection_revision` 并返回两个 current ID。

- [ ] **Step 8: 运行新 API、独立 GSV 和批次 1 API 回归**

```powershell
uv run pytest tests/integration_cpu/test_segment_versions.py `
  tests/integration_cpu/test_late_results.py `
  tests/integration_cpu/test_batch2_api.py `
  tests/integration_cpu/test_api_jobs.py `
  tests/unit/test_pipeline.py -vv -W error
uv run mypy src/voice_pipeline/storage/segment_store.py `
  src/voice_pipeline/storage/version_store.py `
  src/voice_pipeline/api/foundation_routes.py
uv run ruff check src/voice_pipeline/storage src/voice_pipeline/api/foundation_routes.py `
  tests/integration_cpu/test_segment_versions.py tests/integration_cpu/test_late_results.py
```

Expected: 全部 PASS；GSV-only 测试中 reference version ID 和 SHA 不变。

- [ ] **Step 9: 提交**

```powershell
git add src/voice_pipeline/storage/segment_store.py `
  src/voice_pipeline/storage/version_store.py `
  src/voice_pipeline/api/foundation_routes.py `
  src/voice_pipeline/core/job_executor.py src/voice_pipeline/core/pipeline.py `
  tests/integration_cpu/test_segment_versions.py `
  tests/integration_cpu/test_late_results.py `
  tests/integration_cpu/test_batch2_api.py tests/integration_cpu/test_api_jobs.py `
  tests/unit/test_pipeline.py
git commit -m "feat: add immutable segment audio versions"
```

---

### Task 8: 增加 reference/GSV/quality 版本化缓存

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\cache\__init__.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\cache\keys.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\cache_store.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\pipeline.py`
- Create: `D:\TTSsystem-batch2\tests\unit\test_cache_keys.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_cache_integration.py`

**Interfaces:**
- Consumes: Batch 1 requests/fingerprints、ArtifactStore、Database。
- Produces: `ReferenceCacheKeyBuilder`, `GsvCacheKeyBuilder`, `QualityCacheKeyBuilder`, `CacheStore`。

冻结 key 接口：

```python
def build_reference_cache_key(
    request: IndexSynthesisRequest,
    *,
    base_voice_sha256: str,
    engine_fingerprint: EngineFingerprint,
    output_spec: OutputAudioSpec,
) -> CanonicalCacheKey: ...


def build_gsv_cache_key(
    request: GsvSynthesisRequest,
    *,
    engine_fingerprint: EngineFingerprint,
    output_spec: OutputAudioSpec,
) -> CanonicalCacheKey: ...
```

- [ ] **Step 1: 写 key mutation 失败测试**

以合法 baseline payload 为起点，每次只改变一个字段，断言 SHA 改变。reference 覆盖 base voice hash、ref text、每个 vector 元素、fingerprint 每个 hash、seed、output spec；GSV 覆盖 reference audio SHA、prompt text、prompt language、target text/language、speed、seed、fingerprint、output spec。

另断言：

```python
def test_canonicalization_does_not_rescale_emotion_vector() -> None:
    vector = (0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20)
    key = build_reference_key_for(vector)
    assert key.payload["emotion_vector"] == list(vector)
```

- [ ] **Step 2: 写 hit/miss/corruption/random 失败测试**

必须证明：

- identical reference 第二次 Index calls 不增加；
- identical GSV 第二次 GSV calls 不增加；
- 相同 request ID 仍产生不同 job ID；
- cache hit 在新 job 目录生成兼容音频；
- seed<0 永远两次调用引擎；
- blob 被篡改时 entry invalid、重新调用引擎、sentinel 不被覆盖；
- 同 key 不同 canonical JSON 报 `CACHE_KEY_COLLISION`；
- cache hit 仍经过 WAV probe；
- current CAS 使用新 job snapshot，不因 cache hit 绕过迟到保护。

- [ ] **Step 3: 运行测试确认不存在 cache service**

```powershell
uv run pytest tests/unit/test_cache_keys.py `
  tests/integration_cpu/test_cache_integration.py -vv
```

Expected: FAIL。

- [ ] **Step 4: 实现 canonical key**

```python
def canonical_json(payload: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_key(kind: str, payload: Mapping[str, JsonValue]) -> CanonicalCacheKey:
    envelope = {"schema_version": 1, "kind": kind, "payload": dict(payload)}
    serialized = canonical_json(envelope)
    return CanonicalCacheKey(
        kind=kind,
        sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        canonical_payload_json=serialized,
    )
```

不得依赖 Python `hash()`。

- [ ] **Step 5: 实现 CacheStore**

`get_valid()` 在一次读流程中：

1. 取 entry；
2. 比较 canonical payload；
3. resolve blob；
4. 非 symlink、存在、size/SHA/WAV 验证；
5. valid 才返回并增加 hit；
6. 损坏 entry 在短 write transaction 标 invalid 并写 audit。

`put()` 只接受已经 published 的 blob；unique collision 时比较 canonical payload，相同为幂等，不同报错。

cache entry 的主复用单位是 blob。若同一 segment 已存在相同 `complete_cache_key` 的 ready version，可复用该 version ID 并新增 job_artifact；若 segment 不同或 complete snapshot 不同，只复用 blob，并创建属于目标 segment 的新 immutable version metadata。

- [ ] **Step 6: 在唯一 engine 调用点集成缓存**

在 `_invoke_engine()` 之前查询缓存。hit 时不取得 `InferenceLease`、不写 `inference_started`，只写 `state-audit cache_hit` 并 materialize job output；miss 时继续使用批次 1 `_invoke_engine()`，成功后发布 blob/entry。任何其他模块不得直接绕过这个入口调用 adapter。

- [ ] **Step 7: 运行缓存和 engine audit 回归**

```powershell
uv run pytest tests/unit/test_cache_keys.py `
  tests/integration_cpu/test_cache_integration.py `
  tests/unit/test_pipeline.py tests/unit/test_inference_tracker.py -vv -W error
uv run mypy src/voice_pipeline/modules/cache src/voice_pipeline/storage/cache_store.py `
  src/voice_pipeline/core/pipeline.py
uv run ruff check src/voice_pipeline/modules/cache `
  src/voice_pipeline/storage/cache_store.py src/voice_pipeline/core/pipeline.py `
  tests/unit/test_cache_keys.py tests/integration_cpu/test_cache_integration.py
```

Expected: 全部 PASS；cache hit 不产生 fake engine inference event。

- [ ] **Step 8: 提交**

```powershell
git add src/voice_pipeline/modules/cache src/voice_pipeline/storage/cache_store.py `
  src/voice_pipeline/core/pipeline.py tests/unit/test_cache_keys.py `
  tests/integration_cpu/test_cache_integration.py tests/unit/test_pipeline.py `
  tests/unit/test_inference_tracker.py
git commit -m "feat: cache versioned reference and gsv audio"
```

---

### Task 9: 复用 faster-whisper/Silero VAD 和 RapidFuzz 实现 reference 质量门

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\__init__.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\models.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\ports.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\fake.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\faster_whisper.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\modules\quality\text.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\pipeline.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\models\schemas.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\core\config.py`
- Create: `D:\TTSsystem-batch2\config\quality-model.lock.yaml`
- Create: `D:\TTSsystem-batch2\scripts\setup-quality.ps1`
- Create: `D:\TTSsystem-batch2\tests\unit\test_quality_policy.py`
- Create: `D:\TTSsystem-batch2\tests\quality\test_faster_whisper_quality.py`

**Interfaces:**
- Consumes: `AudioResult`, `probe_wav()`, `QualityCacheStore`。
- Produces: `QualityAnalyzer` Protocol、`DeterministicQualityAnalyzer`、`FasterWhisperQualityAnalyzer`、`QualityReport`。

冻结 Protocol：

```python
class QualityAnalyzer(Protocol):
    @property
    def policy_fingerprint(self) -> QualityPolicyFingerprint: ...

    async def analyze_reference(
        self,
        *,
        audio_path: Path,
        expected_text: NonBlankText,
    ) -> QualityReport: ...
```

- [ ] **Step 1: 写文本规范化、阈值边界和报告 schema 失败测试**

```python
def test_normalize_reference_text_is_unicode_deterministic() -> None:
    assert normalize_reference_text(" Ａ，我 还活着！\r\n") == "a我还活着"


@pytest.mark.parametrize(
    ("speech_seconds", "ratio", "similarity", "passed"),
    [
        (1.50, 0.35, 0.60, True),
        (1.49, 0.35, 0.60, False),
        (1.50, 0.34, 0.60, False),
        (1.50, 0.35, 0.59, False),
    ],
)
def test_quality_policy_boundaries(
    speech_seconds: float,
    ratio: float,
    similarity: float,
    passed: bool,
) -> None:
    report = evaluate_quality_metrics(
        metrics=metrics(
            total_seconds=5.0,
            speech_seconds=speech_seconds,
            speech_ratio=ratio,
            normalized_expected="我还活着",
            normalized_transcript="我还活着",
            similarity=similarity,
        ),
        policy=QualityPolicy.v1(),
    )
    assert report.passed is passed
```

还须覆盖总时长 3.0/9.0 包含端、短文本 0.75、NaN 拒绝、空 ASR、VAD 无语音和 quality fingerprint 字段。

- [ ] **Step 2: 写 fake analyzer pipeline 集成失败测试**

测试 reference、segment 和 segment-bound reference 三条路径都调用 analyzer；quality fail 时：

- job failed；
- stable quality error；
- 不创建 cache/version；
- current/旧音频不变；
- Index 已生成的 job 文件可留作诊断；
- GSV 未调用。

- [ ] **Step 3: 运行 unit/integration 测试确认质量模块缺失**

```powershell
uv run pytest tests/unit/test_quality_policy.py `
  tests/unit/test_pipeline.py tests/integration_cpu/test_segment_versions.py -vv
```

Expected: FAIL。

- [ ] **Step 4: 实现 QualityReport 与纯策略**

`QualityReport` 至少包含：

```python
class QualityReport(StrictModel):
    schema_version: Literal[1]
    policy_fingerprint: QualityPolicyFingerprint
    passed: bool
    total_duration_seconds: float
    speech_duration_seconds: float
    speech_ratio: float
    speech_timestamps: tuple[SpeechInterval, ...]
    expected_text: str
    transcript: str
    normalized_expected: str
    normalized_transcript: str
    normalized_text_similarity: float
    detected_language: str | None
    detected_language_probability: float | None
    checks: tuple[QualityCheck, ...]
```

纯策略根据第 2.5 节阈值返回 report 或由 service 抛稳定 `PipelineError`；analyzer 自身不更新数据库/current。

- [ ] **Step 5: 实现 faster-whisper adapter，不复制源码**

adapter：

1. lazy 创建一个 CPU/int8 `WhisperModel`；
2. 从锁定本地 model path 加载，禁止联网 fallback；
3. 调用 pinned faster-whisper 的音频解码和 Silero VAD API；
4. `transcribe(language="zh", beam_size=5, condition_on_previous_text=False, vad_filter=True)`；
5. 迭代 generator 到完成；
6. RapidFuzz 计算规范化 Levenshtein similarity；
7. 把包版本、model revision/file hashes、Silero asset hash、RapidFuzz version、阈值和 normalizer version 纳入 policy fingerprint。

只包装 public/pinned API；不得复制 `vad.py` 或 Whisper 源码。

- [ ] **Step 6: 实现 setup-quality.ps1 和 lock**

脚本：

```powershell
param(
  [string]$Root = 'D:\TTSsystem',
  [switch]$Offline
)
$Revision = '536b0662742c02347bc0e980a01041f333bce120'
$Destination = Join-Path $Root 'runtime\models\faster-whisper-small'
```

在线模式使用 `huggingface_hub.snapshot_download`，显式 repo/revision/local_dir；离线模式只验证。下载后按本计划第 2.5 节的 tracked lock 计算每个 required file 的 size/SHA，任何不符退出非零。脚本第二次运行不得重下或改变 lock，普通 setup 永远不写 tracked lock；不得写 access token。

- [ ] **Step 7: 在 pipeline 中放置质量门**

顺序固定：

```text
Index/cache audio
-> probe_wav
-> quality cache lookup
-> VAD/ASR policy
-> quality_passed audit
-> reference manifest/blob/version/cache commit
-> optional GSV
```

reference manifest 和 job result 向后兼容地增加 `quality_result`。GSV-only 从已有 reference version 读取已经保存的质量报告，不重新 ASR；若 version state 非 ready 则拒绝。

- [ ] **Step 8: 运行 fake quality 全套**

```powershell
uv run pytest tests/unit/test_quality_policy.py tests/unit/test_pipeline.py `
  tests/integration_cpu/test_segment_versions.py `
  tests/integration_cpu/test_cache_integration.py -vv -W error
uv run mypy src/voice_pipeline/modules/quality src/voice_pipeline/core/pipeline.py
uv run ruff check src/voice_pipeline/modules/quality src/voice_pipeline/core/pipeline.py `
  tests/unit/test_quality_policy.py
```

Expected: 全部 PASS。

- [ ] **Step 9: 运行 pinned model contract（资产存在时必须零 skip）**

```powershell
.\scripts\setup-quality.ps1 -Root D:\TTSsystem-batch2
uv run pytest tests/quality/test_faster_whisper_quality.py `
  -m quality_model -vv -W error
```

Expected: 本地资产具备时 PASS。若资产缺失，开发报告准确记录 prerequisite；测试文件本身不得调用 `pytest.skip`，独立 acceptance 把唯一缺资产状态判为 BLOCKED。

- [ ] **Step 10: 提交**

```powershell
git add src/voice_pipeline/modules/quality src/voice_pipeline/core/pipeline.py `
  src/voice_pipeline/models/schemas.py src/voice_pipeline/core/config.py `
  config/quality-model.lock.yaml scripts/setup-quality.ps1 `
  tests/unit/test_quality_policy.py tests/quality/test_faster_whisper_quality.py `
  tests/unit/test_pipeline.py tests/integration_cpu/test_segment_versions.py `
  tests/integration_cpu/test_cache_integration.py
git commit -m "feat: gate references with vad and asr quality"
```

---

### Task 10: 实现保护 current/父引用/在途 job 的两阶段 retention

**Files:**
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\storage\retention.py`
- Create: `D:\TTSsystem-batch2\src\voice_pipeline\api\maintenance_routes.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\storage\recovery.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\cli.py`
- Create: `D:\TTSsystem-batch2\tests\unit\test_retention.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_retention_recovery.py`

**Interfaces:**
- Consumes: `VersionStore`, `ArtifactStore`, DB retention tables。
- Produces: `RetentionPlanner.plan()`, `RetentionExecutor.apply()`, loopback maintenance API/HTTP-only CLI。

冻结接口：

```python
class RetentionPlanner:
    async def plan(self, *, segment_id: UUID | None) -> RetentionPlan: ...


class RetentionExecutor:
    async def apply(self, plan_id: UUID) -> RetentionReceipt: ...
```

- [ ] **Step 1: 写精确保留集合失败测试**

固定场景：

```text
reference r1..r8
active_ref = r1
ready GSV 引用 r2
最新 5 个非 current reference = r4..r8
```

第一次 plan 只能删除 r3；保留 r1(current)、r2(parent reference)、r4..r8(history quota)。另测：

- active GSV + 5 个其他 ready GSV；
- queued/running job 捕获的旧 reference/GSV；
- deleting/corrupt 不计入普通 ready quota；
- 跨 segment 独立配额；
- `history_limit` 只能为 5；
- current 和父引用保护可让总存活数超过 6。

- [ ] **Step 2: 写 dry-run/apply/idempotency/crash 失败测试**

测试：

- plan 不改文件/DB version state；
- apply 要求 `storage_revision` 未变化；
- stale plan 409；
- apply 两次返回同一 receipt；
- Windows PermissionError 保持 deleting，current 不动；
- kill after `deleting` mark，restart 完成；
- kill after move-to-trash，restart 完成 tombstone；
- 多个 version/cache 共用 blob，最后引用消失前 blob 不删；
- cache 超过 500/90 天按 LRU/age 失效，但 current/version protection 优先；
- cleanup failure 不把 synthesis job 改 failed。

- [ ] **Step 3: 运行测试确认 retention 不存在**

```powershell
uv run pytest tests/unit/test_retention.py `
  tests/integration_cpu/test_retention_recovery.py -vv
```

Expected: FAIL。

- [ ] **Step 4: 实现 deterministic planner**

planner 在一致读 transaction 中：

1. 构造所有 current；
2. 构造 queued/running snapshot refs；
3. 为每 segment/type 选最新 5 个“非 current ready”；
4. 先标记候选 GSV；
5. 从 plan 中将会保留的 GSV 构造 parent reference 集合；
6. 再标记 reference；
7. 每个 version 输出 keep/delete + 单一主要 reason；
8. 保存 plan 和 `storage_revision`。

排序固定 `created_at DESC, version_id DESC`，不得依赖目录时间。

- [ ] **Step 5: 实现 two-phase apply**

每个 candidate：

1. 短 DB transaction 再验证不受保护，`ready -> deleting`；
2. 原子 move 该 version manifest 到 `trash/{plan_id}`；当 blob 最后一个受保护引用消失时再 move blob；文件占用则保持 deleting；
3. 短 DB transaction `deleting -> deleted`；
4. blob 无任何 ready/deleting version、valid cache，且没有 queued/running job snapshot 保护后再删除/tombstone；terminal job 的 `job_artifacts` 只保留审计关系，不永久阻止 blob GC；
5. audit 每个阶段；
6. apply 完成后写 immutable receipt。

先处理 GSV，再处理 reference。不得物理删除 version 元数据。

- [ ] **Step 6: 实现 API 和 HTTP-only CLI**

CLI：

```text
voice-pipeline maintenance retention-plan [--segment-id UUID]
voice-pipeline maintenance retention-apply PLAN_ID
voice-pipeline maintenance cache-status
```

CLI 只调用 loopback HTTP，沿用批次 1 JSON stdout/diagnostics stderr/exit code 约定。

- [ ] **Step 7: 运行 retention、API 和 CLI 测试**

```powershell
uv run pytest tests/unit/test_retention.py `
  tests/integration_cpu/test_retention_recovery.py `
  tests/integration_cpu/test_batch2_api.py `
  tests/contract/test_cli_json_contract.py -vv -W error
uv run mypy src/voice_pipeline/storage/retention.py `
  src/voice_pipeline/api/maintenance_routes.py src/voice_pipeline/cli.py
uv run ruff check src/voice_pipeline/storage/retention.py `
  src/voice_pipeline/api/maintenance_routes.py src/voice_pipeline/cli.py `
  tests/unit/test_retention.py tests/integration_cpu/test_retention_recovery.py
```

Expected: 全部 PASS。

- [ ] **Step 8: 提交**

```powershell
git add src/voice_pipeline/storage/retention.py `
  src/voice_pipeline/api/maintenance_routes.py `
  src/voice_pipeline/storage/recovery.py src/voice_pipeline/cli.py `
  tests/unit/test_retention.py tests/integration_cpu/test_retention_recovery.py `
  tests/integration_cpu/test_batch2_api.py tests/contract/test_cli_json_contract.py
git commit -m "feat: retain current and referenced audio versions"
```

---

### Task 11: 集成 app lifecycle、health、doctor、配置和本地运行脚本

**Files:**
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\api\app.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\api\dependencies.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\api\routes.py`
- Modify: `D:\TTSsystem-batch2\src\voice_pipeline\runtime\doctor.py`
- Modify: `D:\TTSsystem-batch2\config\app.fake.yaml`
- Modify: `D:\TTSsystem-batch2\config\app.example.yaml`
- Modify: `D:\TTSsystem-batch2\scripts\start.ps1`
- Modify: `D:\TTSsystem-batch2\scripts\stop.ps1`
- Create: `D:\TTSsystem-batch2\docs\batch-2-storage-runbook.md`
- Modify: `D:\TTSsystem-batch2\README.md`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_app_storage_lifecycle.py`
- Modify: `D:\TTSsystem-batch2\tests\contract\test_doctor.py`
- Modify: `D:\TTSsystem-batch2\tests\process\test_start_stop_scripts.py`

**Interfaces:**
- Consumes: Tasks 3–10 所有组件。
- Produces: production `ControlPlane` composition、冻结启动/关闭顺序、storage/quality health 和 runbook。

- [ ] **Step 1: 写 lifecycle order 失败测试**

用 recording components 断言启动顺序：

```text
control_lock_acquired
database_opened
migrations_at_head
database_quick_check_ok
runtime_started_and_stale_workers_cleaned
jobs_recovered
artifacts_reconciled
quality_preflight_ok
queue_started
dispatcher_started
http_accepting
```

关闭顺序：

```text
http_not_accepting
dispatcher_stopped_or_interrupted
queue_stopped
runtime_stopped
database_closed
control_lock_released
```

所有 stop 接收同一 monotonic absolute deadline，不能每组件重新获得完整预算。

- [ ] **Step 2: 写 persistence health/doctor 失败测试**

`GET /api/v1/health` 新增：

```json
{
  "storage": {
    "status": "ready",
    "database_path": "absolute path",
    "alembic_revision": "0001_batch2_foundation",
    "journal_mode": "wal",
    "quick_check": "ok",
    "artifact_root": "absolute path",
    "missing_ready_versions": 0,
    "corrupt_ready_versions": 0,
    "last_recovery_run_id": "uuid"
  },
  "dispatcher": {
    "state": "running",
    "queued_count": 0,
    "active_job_id": null,
    "recovered_interrupted_count": 0
  },
  "quality": {
    "mode": "fake",
    "status": "ready",
    "policy_fingerprint_sha256": "64 lowercase hex"
  }
}
```

doctor 真实模式验证 quality lock/model path/hash；缺本地模型返回 prerequisite exit 20，不得标 ready。

- [ ] **Step 3: 运行测试确认 app 仍构造内存 registry**

```powershell
uv run pytest tests/integration_cpu/test_app_storage_lifecycle.py `
  tests/contract/test_doctor.py tests/process/test_start_stop_scripts.py -vv
```

Expected: FAIL。

- [ ] **Step 4: 重构 ControlPlane composition**

`ControlPlane.__init__` 使用 Protocol 类型而非 `Any`，持有：

```text
settings
runtime
audit + state_audit
database
job_store
segment_store
version_store
cache_store
artifact_store
quality_analyzer
recovery
retention
queue
dispatcher
service
control_lock
```

`create_app()` 只组装依赖；重 IO 在 lifespan 中按冻结顺序执行。fake/external_test 使用 deterministic quality analyzer；real 强制 faster-whisper analyzer。

- [ ] **Step 5: 更新配置**

`app.fake.yaml` 使用临时/配置 runtime 下：

```yaml
storage:
  database_path: runtime/state/pipeline.sqlite3
  artifact_root: runtime/artifacts
  control_lock_path: runtime/state/control.lock
  busy_timeout_ms: 5000
  wal_autocheckpoint_pages: 1000
  history_limit: 5
  cache_max_entries_per_kind: 500
  cache_max_age_days: 90
quality:
  mode: fake
  policy_version: reference-quality-v1
```

`app.example.yaml` 的 real quality 包含锁定 model path/device/compute type 和第 2.5 节阈值。路径相对配置文件解析后转绝对。

- [ ] **Step 6: 更新 start/stop/doctor**

`start.ps1`：

- 不直接操作 DB/WAL；
- 预检/升级由控制面在 lock 内完成；
- 等待 health 的 storage/dispatcher/quality ready；
- run receipt 增加 database path/alembic revision/control lock metadata。

`stop.ps1`：

- 继续 loopback shutdown；
- 共享 10 秒总预算；
- 验证 DB 关闭后 lock 可重新获取；
- 不删除 WAL/SHM；
- receipt 增加 queued/running/interrupted counts。

- [ ] **Step 7: 写 runbook**

runbook 包含：

- 数据库/Artifact layout；
- online start/stop；
- backup/restore 使用 SQLite backup；
- retention plan/apply；
- quality model offline setup；
- missing/corrupt artifact 诊断；
- interrupted job frozen retry；
- 禁止手删 WAL/SHM、改 DB current、编辑不可变 blob；
- Batch 2 非目标。

- [ ] **Step 8: 运行 lifecycle、doctor、start/stop 回归**

```powershell
uv run pytest tests/integration_cpu/test_app_storage_lifecycle.py `
  tests/contract/test_doctor.py tests/process/test_start_stop_scripts.py `
  tests/integration_cpu/test_api_jobs.py `
  tests/contract/test_cli_json_contract.py -vv -W error
uv run mypy src/voice_pipeline workers
uv run ruff check .
```

Expected: 全部 PASS。

- [ ] **Step 9: 提交**

```powershell
git add src/voice_pipeline/api src/voice_pipeline/runtime/doctor.py `
  config/app.fake.yaml config/app.example.yaml scripts/start.ps1 scripts/stop.ps1 `
  docs/batch-2-storage-runbook.md README.md `
  tests/integration_cpu/test_app_storage_lifecycle.py `
  tests/contract/test_doctor.py tests/process/test_start_stop_scripts.py `
  tests/integration_cpu/test_api_jobs.py tests/contract/test_cli_json_contract.py
git commit -m "feat: integrate durable storage lifecycle"
```

---

### Task 12: 增加真实进程崩溃、故障注入和并发回归

**Files:**
- Create: `D:\TTSsystem-batch2\tests\process\test_crash_recovery.py`
- Create: `D:\TTSsystem-batch2\tests\integration_cpu\test_storage_fault_matrix.py`
- Modify: `D:\TTSsystem-batch2\tests\fixtures\external_harness.py`
- Modify: `D:\TTSsystem-batch2\tests\fixtures\fake_engine_server.py`
- Create: `D:\TTSsystem-batch2\tests\fixtures\sqlite_observer.py`

**Interfaces:**
- Consumes: production HTTP/process/config paths；不 import product internals 做判定。
- Produces: developer process/fault regression，覆盖独立验收前的主要风险。

- [ ] **Step 1: 扩展外部 fake engine 的阻塞/故障能力**

fake server 仍是独立解释器和随机端口，增加由普通 HTTP 控制的：

```text
block next request
release request
return HTTP 500
truncate WAV
emit silence
emit too-short WAV
emit too-long WAV
close after headers
report active/max_active/call hashes
```

server 用 `ThreadingHTTPServer`，使 synth 阻塞时 control endpoint 仍可进入。不得读取 pytest 环境变量。

- [ ] **Step 2: 写 hard-kill recovery 进程测试**

测试流程：

1. 临时 config/DB/artifact root；
2. 启动两个 external fake engines 和 control；
3. 提交 blocked job；
4. GET 确认 running、engine active=1；
5. 记录 control/worker PID + create-time；
6. 强杀 control process tree；
7. 启动同一路径的新 control；
8. 新 control 清理旧 worker、active=0；
9. 旧 job 为 interrupted；
10. retry 新 job 成功；
11. queued job 自动恢复；
12. DB integrity/FK check；
13. stop 后所有 PID/create-time 已退出。

- [ ] **Step 3: 写 fault matrix**

参数化：

```text
engine HTTP 500
engine timeout
truncated/silent/short/long WAV
quality mismatch
DB external write lock <= 1 second
DB external write lock > busy timeout
artifact destination sentinel
cache blob corruption
kill at artifact_staged
kill at artifact_published
kill at version_committed
kill at cleanup_candidate
kill at cleanup_file_removed
```

每个场景断言旧 current 文件 SHA 不变、无 dangling ready row/path、无 owned partial、稳定 error、后续 job 可成功。`DATABASE_BUSY` 可返回 retryable 稳定错误，但不能泄露原生 `database is locked` traceback。

- [ ] **Step 4: 写 24 并发挑战**

并发混合：

- 12 job submissions；
- 4 activations；
- 4 cancel/retry；
- 2 retention plans；
- 2 cache status reads。

断言：

```text
all HTTP responses are contract-valid
no 500
SQLite quick_check=ok
foreign_key_check empty
GPU/fake engine max_active_observed=1
one current per type
no terminal job changes afterward
```

- [ ] **Step 5: 运行 process/fault suites 三次排查时序不稳定**

```powershell
1..3 | ForEach-Object {
  uv run pytest tests/process/test_crash_recovery.py `
    tests/integration_cpu/test_storage_fault_matrix.py `
    -vv -W error
  if ($LASTEXITCODE -ne 0) { throw "fault suite run $_ failed" }
}
```

Expected: 三次全部 PASS、零 skip/xfail。

- [ ] **Step 6: 运行 Batch 1 CPU 黑盒回归**

```powershell
uv run pytest tests/unit tests/contract tests/integration_cpu tests/process `
  -m 'not gpu and not gpu_residency and not quality_model' `
  -vv -W error
```

Expected: 全部 PASS；原 CLI/HTTP、独立 GSV、原子输出、进程隔离和单 GPU 互斥无回归。

- [ ] **Step 7: 提交**

```powershell
git add tests/process/test_crash_recovery.py `
  tests/integration_cpu/test_storage_fault_matrix.py `
  tests/fixtures/external_harness.py tests/fixtures/fake_engine_server.py `
  tests/fixtures/sqlite_observer.py
git commit -m "test: cover storage crashes and concurrency"
```

---

### Task 13: 完成开发者验证、文档、最终 commit 和 handoff

**Files:**
- Modify: `D:\TTSsystem-batch2\README.md`
- Create at run time, Git ignored: `D:\TTSsystem-batch2\runtime\handoff\batch2-developer-report.json`

**Interfaces:**
- Consumes: Tasks 0–12。
- Produces: clean immutable commit、wheel、机器可读开发报告；不产生最终验收判定。

- [ ] **Step 1: 验证规范映射**

在 README/runbook 中建立表：

```text
Batch2 requirement -> implementation module -> developer test -> independent gate
SQLite models -> storage/* -> database/persistent tests -> C
recovery -> dispatcher/recovery -> process crash test -> D
cancel/retry -> dispatcher/routes -> cancel retry test -> E
immutable/current -> version store -> segment version test -> F/G
cache/retention -> cache/retention -> cache/cleanup tests -> H
quality -> modules/quality -> quality tests -> I/quality
Batch1 compatibility -> existing APIs -> regression suite -> J
```

- [ ] **Step 2: 运行静态和完整 CPU suite**

```powershell
$ErrorActionPreference = 'Stop'
uv sync --frozen --extra dev --python 3.11
uv lock --check
uv run python -m compileall -q src workers
uv run ruff format --check .
uv run ruff check .
uv run mypy src/voice_pipeline workers
uv run pytest tests `
  -m 'not gpu and not gpu_residency and not quality_model' `
  -vv -W error --strict-config --strict-markers `
  --cov=voice_pipeline --cov=workers.indextts2 --cov-branch `
  --cov-fail-under=85 `
  --junitxml=runtime/batch2-developer-tests.xml `
  --cov-report=xml:runtime/batch2-coverage.xml
if ($LASTEXITCODE -ne 0) { throw 'batch2 CPU suite failed' }
```

Expected: 零 fail、零 skip、零 xfail，branch coverage >= 85%。

- [ ] **Step 3: 构建 wheel 并在 clean venv 验证 packaged migrations**

```powershell
uv build --wheel --out-dir runtime/dist-batch2
$Wheel = (Get-ChildItem runtime/dist-batch2/*.whl | Select-Object -Single).FullName
uv venv runtime/clean-batch2 --python 3.11
uv pip install --python runtime/clean-batch2/Scripts/python.exe $Wheel
& runtime/clean-batch2/Scripts/python.exe -m voice_pipeline --help
& runtime/clean-batch2/Scripts/python.exe -c `
  "from importlib.resources import files; print(files('voice_pipeline.storage.migrations'))"
if ($LASTEXITCODE -ne 0) { throw 'clean wheel verification failed' }
```

- [ ] **Step 4: 验证真实 quality 资产（存在时）**

```powershell
if (Test-Path runtime/models/faster-whisper-small/model.bin) {
  .\scripts\setup-quality.ps1 -Root (Get-Location).Path -Offline
  uv run pytest tests/quality/test_faster_whisper_quality.py `
    -m quality_model -vv -W error `
    --junitxml=runtime/batch2-quality-tests.xml
  if ($LASTEXITCODE -ne 0) { throw 'real quality tests failed' }
}
```

缺失本地模型只能写入开发报告的 `known_prerequisites`，不能把 tracked lock/adapter/test 缺失写成资产 BLOCKED。

- [ ] **Step 5: 提交最终 tracked 变更并冻结 SHA**

```powershell
git add README.md docs config pyproject.toml uv.lock src scripts tests
git commit -m "docs: complete batch two developer handoff"
$FinalSha = (git rev-parse HEAD).Trim()
$Dirty = git status --short
if ($Dirty) { throw "working tree is not clean`n$Dirty" }
```

- [ ] **Step 6: 生成 machine-readable developer report**

report 必含：

```json
{
  "schema_version": 1,
  "commit_sha": "40 lowercase hexadecimal characters",
  "batch1_baseline_sha": "40 lowercase hexadecimal characters",
  "product_root": "D:\\TTSsystem-batch2",
  "wheel_path": "absolute path",
  "wheel_sha256": "64 lowercase hexadecimal characters",
  "alembic_revision": "0001_batch2_foundation",
  "database_schema_sha256": "64 lowercase hexadecimal characters",
  "reuse_inventory_sha256": "64 lowercase hexadecimal characters",
  "quality_model_revision": "536b0662742c02347bc0e980a01041f333bce120",
  "cpu_test_summary": {
    "passed": 1,
    "failed": 0,
    "skipped": 0,
    "xfailed": 0,
    "branch_coverage_percent": 85.0
  },
  "quality_test_summary": {
    "status": "passed or prerequisite_missing",
    "junit_path": "absolute path or null"
  },
  "known_prerequisites": [],
  "known_failures": []
}
```

数值使用实际结果而非示例。report 写入 Git ignored `runtime/handoff`，再确认 report SHA 等于 HEAD、Batch1 SHA 是祖先、Git 干净。

- [ ] **Step 7: 停止在验收边界**

开发智能体只报告：

```text
final commit SHA
batch1 baseline SHA
developer report path
wheel path/SHA
quality prerequisite state
known failures
```

开发智能体不得宣布 Batch 2 PASS，不得创建或修改主智能体的 `.acceptance/batch2_blackbox`。

---

## 6. 由主智能体执行的独立验收

开发智能体完成 Task 13 后停止。以下 harness、随机 challenge、证据校验和最终判定由当前主智能体执行，不能交给开发智能体自验。

### 6.1 验收工具隔离

独立 harness：

```text
D:\TTSsystem\.acceptance\batch2_blackbox\
├── test_harness_self.py
├── conftest.py
├── launch.py
├── http_client.py
├── process_guard.py
├── sqlite_observer.py
├── artifact_inventory.py
├── fake_engine_server.py
├── test_persistence.py
├── test_crash_recovery.py
├── test_cancel_retry.py
├── test_versions_current.py
├── test_late_reference.py
├── test_retention_cache.py
├── test_faults_concurrency.py
├── test_real_quality_gpu.py
└── verify_evidence.py
```

规则：

- harness 在开发者最终 commit 后创建，并保持 Git ignored；
- assertions 使用标准库 `sqlite3`、hashlib、OS process/PID create-time 和 HTTP；
- 不 import `voice_pipeline.storage`、`VersionStore`、`RetentionPlanner` 或产品判断函数；
- fake engines 是 harness 自己的外部进程、随机端口、随机 UUID 和动态文本；
- 产品不得读取 `PYTEST_CURRENT_TEST`、`.acceptance` 路径、特定 challenge 文本或提供验收成功捷径；
- 强杀和清理按 PID + create-time，避免 PID reuse；
- 每条 native 命令先保存真实 `$LASTEXITCODE`，再写日志，不能被 `Tee-Object` 覆盖；
- 验收运行开始后 tracked source、lock、migration 或配置发生变化，当前证据立即作废。

### 6.2 证据目录

```text
{product_root}/runtime/acceptance/batch2/{commit-sha}/{run-id}/
├── frozen-delivery.json
├── command-exit-codes.jsonl
├── harness-hashes.json
├── harness-self.xml
├── developer-tests.xml
├── coverage.xml
├── wheel/
│   ├── package.whl
│   └── sha256.txt
├── http-transcript.jsonl
├── engine-audit.jsonl
├── state-audit.jsonl
├── process-events.jsonl
├── sqlite/
│   ├── schema.json
│   ├── integrity.txt
│   ├── foreign-key-check.json
│   ├── jobs.json
│   └── versions-and-pointers.json
├── artifacts/
│   ├── inventory-before.json
│   ├── inventory-after.json
│   └── cleanup-receipts.jsonl
├── gates/
│   ├── A-delivery/
│   ├── B-clean-build/
│   ├── C-persistence/
│   ├── D-crash-recovery/
│   ├── E-cancel-retry/
│   ├── F-versions-current/
│   ├── G-late-reference/
│   ├── H-retention-cache/
│   ├── I-fault-quality-concurrency/
│   └── J-batch1-regression/
└── final-disposition.json
```

artifact inventory 每行至少含：

```text
relative_path
version_id
artifact_type
segment_id
content_sha256
byte_size
state
is_current
ready_gsv_reference_count
inflight_job_reference_count
cache_reference_count
```

### 6.3 冻结交付和 clean wheel

PowerShell 骨架：

```powershell
$ErrorActionPreference = 'Stop'
$DeveloperReport = 'D:\TTSsystem-batch2\runtime\handoff\batch2-developer-report.json'
$Dev = Get-Content -LiteralPath $DeveloperReport -Raw | ConvertFrom-Json
$ProductRoot = [System.IO.Path]::GetFullPath([string]$Dev.product_root)
Set-Location -LiteralPath $ProductRoot
$Commit = (& git rev-parse HEAD).Trim()
$RunId = [guid]::NewGuid().ToString()
$Evidence = Join-Path $ProductRoot "runtime\acceptance\batch2\$Commit\$RunId"
$Harness = 'D:\TTSsystem\.acceptance\batch2_blackbox'
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

if (git status --short) { throw 'tracked worktree must be clean' }
uv lock --check
uv sync --frozen --extra dev --python 3.11
uv run ruff format --check .
uv run ruff check .
uv run mypy src/voice_pipeline workers
uv build --wheel --out-dir "$Evidence\wheel"

uv run pytest tests `
  -m 'not gpu and not gpu_residency and not quality_model' `
  -vv -W error --strict-config --strict-markers `
  --cov=voice_pipeline --cov=workers.indextts2 --cov-branch `
  --cov-fail-under=85 `
  "--cov-report=xml:$Evidence\coverage.xml" `
  "--junitxml=$Evidence\developer-tests.xml"
if ($LASTEXITCODE -ne 0) { throw 'developer test gate failed' }

uv venv "$Evidence\clean-venv" --python 3.11
$CleanPython = "$Evidence\clean-venv\Scripts\python.exe"
$Wheel = (Get-ChildItem "$Evidence\wheel\*.whl" | Select-Object -Single).FullName
uv pip install --python $CleanPython $Wheel pytest httpx psutil
```

独立 harness 自测必须有至少三个 mutant fixtures，证明它会拒绝：

1. 迟到结果覆盖 current；
2. retention 删除 parent reference；
3. crash 后 running 永不 interrupted。

```powershell
& $CleanPython -m pytest "$Harness\test_harness_self.py" -vv `
  "--junitxml=$Evidence\harness-self.xml"
if ($LASTEXITCODE -ne 0) { throw 'harness self-test failed' }
```

---

### Gate A：冻结交付、基线祖先、锁和迁移

验证：

- developer report schema/commit/wheel SHA；
- Batch1 acceptance receipt 为 PASS 且其 SHA 是当前 commit 祖先；
- Git clean、tracked 文件清单/hash；
- wheel SHA 和从当前 commit 重建 wheel SHA；
- `uv.lock` frozen；
- packaged Alembic head 唯一；
- `config/open-source-reuse.yaml` 的每个 adopted pin 与 lock 一致；
- `config/quality-model.lock.yaml` tracked、schema 有效；
- 不存在 Redis/Celery/公网绑定/产品验收特判。

以下缺失直接 FAIL：migration、tracked lock、reuse entry、schema、测试或 developer report。

### Gate B：clean install、静态检查和开发测试

要求：

- Python 3.11 clean venv 只从 wheel 安装；
- ruff format/check、strict mypy、compileall 全过；
- CPU suite 零 fail/skip/xfail；
- branch coverage >=85%；
- wheel 外部可发现 packaged migration；
- harness self-test 全过；
- `voice-pipeline --help` 和 fake doctor 在仓库外运行。

### Gate C：持久化与正常重启

harness 用 HTTP：

1. 建 task/segment；
2. 完成成功/失败 job；
3. 生成多个 reference/GSV version；
4. 记录 current、snapshots、errors、音频 SHA；
5. 正常 stop；
6. 使用同 DB/artifact root 重启；
7. 逐项比较。

必须证明：

- task/segment/job/version/current/cache 都保留；
- job/audio/manifest/version audio URL 仍有效；
- 相同 request ID 产生不同 job ID；
- `PRAGMA integrity_check` 为 `ok`；
- `PRAGMA foreign_key_check` 零行；
- journal mode 为 WAL；
- 第二控制面无法同时持有同一 lock/DB。

### Gate D：硬崩溃恢复

1. external fake engine 阻塞 reference job；
2. GET 确认 job running、active=1；
3. 按 PID/create-time 强杀 control；
4. 用相同 storage 启动新 control；
5. 观察旧 worker 全部退出/active=0 后才 accepting；
6. 旧 job 必须 interrupted；
7. queued job FIFO 恢复；
8. retry 创建新 job 并成功；
9. 无 job 永远滞留 running；
10. SQLite/artifact 协调无 dangling ready path。

### Gate E：queued/running cancel、retry 与线性化

分别挑战：

- queued cancel；
- running cancel；
- cancel-vs-engine-success barrier race；
- failed retry；
- interrupted retry；
- cancelled retry；
- succeeded retry 拒绝；
- cancel terminal 幂等。

必须证明：

- queued cancel 不进引擎；
- running cancel 先 abort 并确认 active=0；
- 原终态不再变化；
- retry 新 job、同 request ID、原 snapshot、正确 lineage/attempt；
- success/cancel 竞态只有一个合法线性化结果；
- 已完成 artifact 不因取消被删除；
- abort unknown 时 queue fail-closed。

### Gate F：不可变版本和 current

挑战：

- 连续生成 8 版；
- GET/试听历史；
- 激活最旧版；
- 重启；
- 跨 segment/type 激活；
- 激活 missing/corrupt/deleting；
- 预置 output sentinel；
- 外部尝试修改旧 blob。

必须证明：

- 已发布旧音频 SHA 永不变化；
- GET/试听不改 current；
- current 只指向同 segment、正确 type、ready 文件；
- illegal activation 409 且指针不动；
- failed job 不改 current；
- job output 路径仍满足 Batch1；
- version metadata payload UPDATE 被 DB trigger 拒绝。

### Gate G：迟到结果与 GSV→reference 永久绑定

reference challenge：

```text
snapshot revision=5/current=r1
J running
用户激活 r0 或 patch 到 revision=6
J 生成 r2
```

期望 r2 ready/history，current 保持用户后来选择，`activation_outcome=history_only`。

GSV challenge：

```text
submit with ref=r1/current-gsv=g0
J running
用户切换 ref=r2 并再次选择 g0
J 生成 g1
```

期望 g1 永久保存 `ref_version_id=r1` 和 r1 SHA，进入 history，不覆盖 g0。不得先覆盖再补救。

### Gate H：最近 5 版、父引用保护和缓存

固定 retention challenge：

```text
r1..r8
active_ref=r1
ready GSV references r2
newest five non-current references=r4..r8
```

apply 后只能删除 r3；r1、r2、r4..r8 保留。再移除 r2 的存活 GSV 引用并切 current，下一计划才可重新判断 r1/r2。

另验证：

- dry-run 不改状态；
- stale plan 拒绝；
- apply 幂等；
- current、parent、queued/running snapshot 在 quota 外保护；
- Windows 文件占用可重入；
- cleanup kill/restart 收敛；
- shared blob 最后引用前不删；
- identical deterministic request cache hit 不调用模型；
- cache hit 仍新 job ID/兼容 job output；
- prompt text/ref hash/seed/model fingerprint/output spec 任一变化均 miss；
- random seed bypass；
- corrupt cache invalidates and recomputes；
- cache/quality policy version进入 key。

### Gate I：故障、24 并发和真实 quality

故障注入：

```text
HTTP 500
timeout
connection reset/truncated stream
silent/short/long/truncated WAV
VAD no speech
ASR text mismatch
DB short/long external lock
artifact sentinel/conflict
cache corruption
kill at each state-audit publication event
```

要求：

- 旧 current/旧文件不受损；
- 无 500/unhandled traceback；
- 无 dangling ready row/path/owned partial；
- stable error envelope；
- 故障后下一 job 成功；
- `max_active_observed=1`；
- 24 混合并发请求全部 contract-valid；
- quick_check/FK check 通过。

真实 quality 子门：

1. offline 验证 pinned faster-whisper model hashes；
2. 使用 Batch1 已验收的真实中文 reference 样例和两个动态新样例；
3. VAD speech/timing、ASR transcript/similarity、policy fingerprint 完整；
4. 好样例通过；
5. harness 自制 silence/错文本/截断样例失败；
6. real mode 没有 fake/skip fallback。

### Gate J：Batch 1 全回归

由于 Task 8/9 修改了模型调用前后的 pipeline，按批次 1 规则重跑：

- Batch1 A–F 全部独立 challenge；
- real Index→GSV zh-ja/zh-en objective GPU gate；
- cache miss 实际发生 GPU 推理；
- 第二次相同请求 cache hit 不发生 GPU 推理；
- queue max concurrency 仍为 1；
- independent GSV 仍不改变 reference ID/SHA；
- start/stop、三解释器、fingerprint、audit 无回归；
- 向用户展示最终 zh-ja/zh-en 音频，完成 Batch1 Gate H 的试听确认。

任何 Batch1 A–F 回归是 FAIL。真实 GPU/checkpoint或用户试听尚未具备时只能 BLOCKED，不能 PASS。

---

## 7. 最终判定规则

```text
NOT READY
= Batch1 尚未冻结并由主智能体验收为 PASS，Batch2 实现不得开始。

PASS
= Gate A–J 全部通过，包含真实 quality、Batch1 real GPU 回归和用户试听。

BLOCKED
= 所有不依赖外部资产的 Gate 全部通过，
  唯一缺项是未跟踪的真实 GPU/checkpoint、
  pinned ASR 模型本体、用户黄金音色/mapping 或用户试听；
  prerequisite probe 必须精确退出 20 并逐项列出缺失。

FAIL
= 任一已有前提下的功能、契约、一致性、安全边界或验收工具不满足。
```

以下一律 FAIL，不能降为 BLOCKED：

- 缺 tracked migration、env/reuse/quality lock、schema、runbook 或测试；
- SQLite corruption、foreign key violation、raw `database is locked` traceback；
- current 指向错误 segment/type/state 或缺失文件；
- current/父 reference/在途 job 保护版本被清理；
- 迟到结果覆盖用户后来选择；
- 取消后 engine 仍 active；
- abort unknown 后 queue 继续消费；
- artifact/cache 覆盖 sentinel 或返回损坏音频；
- 重复 request ID 覆盖旧 job；
- terminal job 被重新打开；
- test skip/xfail；
- 产品代码含验收路径/文本/pytest 特判；
- Batch1 CPU/contract 回归失败。

最终 `final-disposition.json` 必须绑定：

```text
Batch2 commit SHA
Batch1 accepted baseline SHA
wheel SHA
harness tree SHA
uv.lock SHA
migration head/schema SHA
reuse/quality lock SHA
每个 Gate 的 PASS/BLOCKED/FAIL
evidence relative paths
真实 prerequisite inventory
用户试听原始反馈
主智能体签字时间
```

只有当前主智能体可以签发最终判定。

---

## 8. 给开发智能体的交接提示词

```text
请执行：
D:\TTSsystem\docs\superpowers\plans\
2026-08-07-batch-2-reliable-jobs-and-artifact-versions.md

先执行 Task 0。若批次1 acceptance receipt 不是完整 PASS，立即报告
NOT READY，不接触当前批次1脏工作树。

批次1通过后，从其 accepted SHA 创建 D:\TTSsystem-batch2 独立 worktree，
按 Task 1–13 严格 TDD、逐任务提交。GitHub/上游已有成熟能力必须优先
按固定版本复用；不得自行重写 ORM、migration、VAD、ASR、Levenshtein、
OS file lock 或模型服务。

保持批次1全部 HTTP/CLI/三环境/单GPU/O_EXCL/abort 契约。SQLite 是 durable
事实源，文件先发布、DB最后提交；retry 新建job；迟到结果只进历史；
current、GSV父reference和在途job受retention保护。

完成 Task 13 后只提交 developer report、commit/wheel SHA 和缺失 prerequisite，
不要自行宣布 PASS，也不要创建主智能体独立验收 harness。
```

---

## 9. 上游依据

- [SQLAlchemy releases](https://github.com/sqlalchemy/sqlalchemy/releases)
- [SQLAlchemy SQLite transaction/foreign-key guidance](https://docs.sqlalchemy.org/en/21/dialects/sqlite.html)
- [SQLAlchemy AsyncSession concurrency guidance](https://docs.sqlalchemy.org/en/21/orm/extensions/asyncio.html)
- [Alembic](https://github.com/sqlalchemy/alembic)
- [Alembic SQLite batch migrations](https://alembic.sqlalchemy.org/en/latest/batch.html)
- [aiosqlite](https://github.com/omnilib/aiosqlite)
- [SQLite WAL](https://sqlite.org/wal.html)
- [portalocker](https://github.com/wolph/portalocker)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Silero VAD](https://github.com/snakers4/silero-vad)
- [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)
- [Pinned faster-whisper-small model](https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120)
