# Emotion Driven Cross-Language TTS Pipeline — Batches 1–2

批次 1 交付一个可重复安装、可自动测试的单段配音闭环：固定中文参考文本与合法 8 维情绪向量经 IndexTTS2 生成参考音频，再由 GPT-SoVITS 生成日语或英语目标语音。

> 本 README 为开发交付文档；正式验收判定由主智能体独立执行，见 `docs/superpowers/plans/2026-08-07-batch-1-dual-engine-core.md` 的验收门 A–H。

## 目录

- [架构](#架构)
- [快速开始（fake 模式）](#快速开始fake-模式)
- [真实模式](#真实模式)
- [CLI](#cli)
- [错误码](#错误码)
- [测试分层](#测试分层)
- [非目标](#非目标)

## 架构

三独立 Python 3.11 环境 + 单进程 FastAPI 控制面：

```text
CLI ──HTTP──> FastAPI 控制面 (127.0.0.1:8765, workers=1)
                 │  单一 asyncio consumer 串行 GPU 推理
                 ├──HTTP──> IndexTTS2 worker (127.0.0.1:9871, 独立 env)
                 └──HTTP──> GPT-SoVITS api_v2.py (127.0.0.1:9880, 独立 env)
```

- 控制面只通过 HTTP adapter 调用引擎，不导入任何模型包；
- CLI 只调用控制面，不能绕过队列直连引擎；
- 全部 GPU 工作经过 `asyncio.Queue` + 恰好一个 consumer；
- 批次 2 将任务、分块、版本、缓存与保留策略写入本地 SQLite；控制面重启会将中断的
  运行任务标记为 `interrupted`，排队任务保留等待重新调度。

### 引擎 pin

| 组件 | 固定 commit / revision |
|---|---|
| IndexTTS2 源码 | `90ca4d608209584bad3a5bd5becc0b80c146e60f` |
| IndexTTS-2 模型 | `740dcaff396282ffb241903d150ac011cd4b1ede` |
| GPT-SoVITS 源码 | `d523079fc05d9a8028d6085bffe4a2757c32abb6` |
| GPT-SoVITS pretrained | `4fae8ec36d3d0373864e580b5d8acfba8da29630`（`pretrained_models.zip` SHA-256 `82881ee0…58793`） |

## 快速开始（fake 模式）

```powershell
Set-Location 'D:\TTSsystem'
uv sync --extra dev --python 3.11
uv run voice-pipeline serve --config config/app.fake.yaml
# 另一终端：
uv run voice-pipeline synthesize-segment --server http://127.0.0.1:8765 --request request.json --output-dir runtime\out --json
```

`fake` 模式使用进程内确定性客户端，不加载任何模型。

## 真实模式

```powershell
pwsh -NoProfile -File scripts/setup-control.ps1
pwsh -NoProfile -File scripts/setup-indextts.ps1
pwsh -NoProfile -File scripts/setup-gpt-sovits.ps1
pwsh -NoProfile -File scripts/setup-quality.ps1 -Root (Get-Location).Path
pwsh -NoProfile -File scripts/start.ps1 -Config D:\TTSsystem\config\acceptance.gpu.local.yaml -Json
uv run voice-pipeline doctor --server http://127.0.0.1:8765 --json
pwsh -NoProfile -File scripts/stop.ps1 -RunFile D:\TTSsystem\runtime\run\processes.json -ReceiptPath runtime\run\stop-receipt.json -Json
```

真实本地配置必须以 `.local.yaml` 结尾（Git 忽略），绝对路径只出现在 `.local.yaml` 中。

## CLI

```powershell
voice-pipeline serve --config CONFIG_PATH
voice-pipeline doctor --server SERVER_URL --json
voice-pipeline generate-reference --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline generate-gsv --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline synthesize-segment --server SERVER_URL --request REQUEST_JSON --output-dir OUTPUT_DIR --json
```

`--json` 时 stdout 只有恰好一个 JSON 对象；日志与进度只写 stderr。

## 任务输出目录

所有任务输出以 `runtime_dir/jobs/<job_id>/` 为工作目录（`runtime_dir` 由配置决定）：

```text
runtime_dir/jobs/<job_id>/
├── reference.wav              # Index 生成的参考音频（已发布）
├── reference-manifest.json    # 参考绑定清单（含 SHA-256）
├── run-manifest.json          # 整段运行清单（含 SHA-256、GPU 时间）
├── target.wav                 # GSV 生成的目标音频（已发布）
└── *.partial.wav / *.working* # 未完成产物，失败/完成时清理
```

- 输出通过 `O_EXCL` 零字节保留 + `os.replace` 原子发布，失败不泄漏半成品；
- CLI `--output` / `--output-dir` 是最终交付位置；参考 CLI 额外生成
  `<output>.reference-manifest.json` portable 清单；
- 同名任务请求 `request_id` 必须不同，否则按 `OUTPUT_CONFLICT` 拒绝。

## 批次 2：本地状态与版本

- SQLite 位于 `runtime/state/pipeline.sqlite3`，其 `-wal` 和 `-shm` 文件由 SQLite
  管理，不能手动删除；
- 音频 blob 位于 `runtime/artifacts/blobs/sha256/`，版本清单位于
  `runtime/artifacts/manifests/versions/`，两者均不可编辑；
- 每个分块的 reference/GSV 版本不可变；`active_ref_version_id` 与
  `active_gsv_version_id` 只会指向 `ready` 版本；
- `GET /api/v1/health` 同时报告 `storage`、`dispatcher` 与 `quality` 状态；
- 本地 GSV 模型配置通过 `/api/v1/model-profiles` 导入和激活。分块 GSV 任务提交时
  冻结已选 profile 的 ID、文件哈希和引擎指纹；之后切换当前 profile 不会影响该任务；
- 参考音频要先通过时长、VAD 与 ASR 文本对齐门禁，才会成为可激活版本。

管理与恢复操作见 `docs/batch-2-storage-runbook.md`。

## 错误码

```text
0 = success
2 = invalid input/config
3 = control plane or engine unavailable
4 = inference/audio validation failure
5 = queue or inference timeout
```

| 错误码 | 含义 |
|---|---|
| `INVALID_INPUT` | 输入非法 |
| `CONFIG_INVALID` | 配置非法 |
| `CONTROL_PLANE_UNAVAILABLE` | 控制面不可达 |
| `ENGINE_UNAVAILABLE` | 引擎不可达 |
| `INDEX_ENGINE_ERROR` / `INDEX_TIMEOUT` | Index 引擎错误/超时 |
| `GSV_ENGINE_ERROR` / `GSV_TIMEOUT` | GSV 引擎错误/超时 |
| `INVALID_AUDIO` / `AUDIO_SILENT` / `REFERENCE_DURATION_OUT_OF_RANGE` | 音频门禁失败 |
| `QUEUE_TIMEOUT` | 排队超时 |
| `OUTPUT_CONFLICT` | 输出目标冲突 |

## 测试分层

```text
tests/unit            # 纯逻辑单测
tests/contract        # HTTP 契约（adapter/worker/doctor/CLI JSON）
tests/integration_cpu # 进程内 API 全链路 + 故障矩阵
tests/process         # 真实子进程/跨解释器/启动停止
tests/gpu             # 真实 GPU 黄金回归（需 GPU 资产与 .local 配置）
```

```powershell
uv run pytest tests/unit tests/contract tests/integration_cpu tests/process -m "not gpu" -vv -W error --cov=voice_pipeline --cov=workers.indextts2 --cov-branch --cov-fail-under=85
```

## 生命周期选择

`resident` 与 `exclusive_process` 共享同一 API 与测试，`config/app.*.yaml` 中
`engine_lifecycle` 决定：

- **`resident`**：Index 与 GSV 两个 worker 常驻，`ensure_engine` 直接复用；
- **`exclusive_process`**：同一时刻只允许一个 worker 运行，切换引擎时先停止另一个
  （进程树终止 + PID registry 更新）。

选择方式：先用 `scripts/probe-engine-lifecycle.ps1` 探测双驻留显存预算
（`combined_peak + required_reserve <= total_mib` 才允许 `resident`）；16GB 显存
不足时真实配置固定使用 `exclusive_process`。`doctor` 输出会校验生命周期下 worker
状态组合是否合法。

## 非目标

批次 1 明确不包含：LLM API、长文本分块/整篇拼接、SQLite/缓存/版本历史、WebUI/SSE/多用户、VAD/ASR/文本对齐、自动改写参考文本、训练模型、修改 GPT-SoVITS 官方 `api_v2.py`。

## 批次 3：整篇自动配音

章节任务通过 OpenAI 兼容的 `/chat/completions` API 完成原文字符区间分块。服务端只接受
LLM 的区间、情绪与中文参考文本；每段 `source_text`/`synthesis_text` 总是从原文
`[source_start, source_end)` 切片取得，不会采用 LLM 改写正文。

真实配置增加：

```yaml
llm:
  mode: openai
  base_url: https://api.openai.com/v1
  model: gpt-4.1-mini
  api_key_env: OPENAI_API_KEY
  timeout_seconds: 60
  max_retries: 2
  max_reference_corrections: 2
```

调用前在控制面进程环境中设置变量（示例中没有也不应写入密钥）：

```powershell
$env:OPENAI_API_KEY = '<your-key>'
voice-pipeline synthesize-chapter --server http://127.0.0.1:8765 --request chapter.json --output-dir runtime\chapter-out --json
```

`chapter.json` 至少包含 `request_id`、`title`、`source_text`、`target_language`、绝对
`base_voice_path` 和已导入的 `model_profile_id`。命令通过 HTTP 等待章节运行，然后原子
下载 `final.wav` 和 `timeline.json`；同名输出绝不覆盖。章节状态可从
`GET /api/v1/chapters/{run_id}` 查询，成功后可下载 `/audio` 与 `/timeline`。

## 批次 4：本地 WebUI 工作台

控制面启动后，在同一台机器浏览器打开 `http://127.0.0.1:8765/`。页面没有 CDN、在线
遥测或浏览器端模型调用；它只访问本地 `/api/v1`，并且不会显示 API key、模型绝对路径或
artifact 文件路径。

- 顶部表单可提交批次 3 的整篇任务，左侧列出章节及其分块进度，右侧可以试听当前
  reference/GSV WAV；
- 可编辑中文参考文本、合成文本、速度、停顿、种子和八维情绪向量。保存仅更新草稿，绝不
  自动排队推理；“恢复 LLM 值”只将当前向量复原为该段已保存的 LLM 向量；
- 任务进度通过章节 SSE 更新。连接中断时页面回退为本地 REST 刷新；
- 历史版本激活、reference/GSV 局部重生成和显式重拼接属于批次 5，不在这个工作台版本中。

## 批次 5：局部重生成、历史与重拼接

工作台现在为每段提供三个**显式**命令：

- **重新生成参考音频**：只冻结当前中文参考草稿、情绪向量和 seed 并提交 IndexTTS2；成功后
  才切换当前 reference，绝不自动提交 GSV。
- **重新生成 GSV**：只冻结点击瞬间的 `active_ref_version_id`、朗读文本、速度和 seed；不调用
  IndexTTS2，也不会应用尚未生成的 reference 草稿。因此可在保留同一参考音频时多次配音。
- **重新生成两者**：先完成并激活 reference，随后才提交 GSV。后段失败时新 reference 保留、旧
  GSV 仍可试听且会标示为过期。

编辑器的版本历史只提供本地音频 URL、冻结输入摘要、质量结果及“设为当前/恢复参数”；不泄露
文件系统路径。切换版本和任何草稿编辑不会自动合成章节。只有点击顶栏“重新拼接整篇”才会按
当前每段 `active_gsv_version_id`、顺序和段后停顿生成新的 `final.wav`/timeline。

所有异步结果仍受 revision 和 selection CAS 保护：用户在推理期间修改草稿或切换版本时，迟到的
输出只保留为历史版本，不覆盖当前选择。
