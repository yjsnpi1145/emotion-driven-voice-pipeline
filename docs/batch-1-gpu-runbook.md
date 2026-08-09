# Batch 1 GPU 运行手册

本手册描述如何在**真实 GPU + 真实模型资产**就绪后执行 Batch 1 的 GPU 黄金验收。
开发者交付状态（无资产时）为 **BLOCKED**，不能以 fake 代替。

## 1. 前置条件（全部为本地真实资产）

| 资产 | 路径约定 | 缺失后果 |
| --- | --- | --- |
| IndexTTS2 模型检查点 | `config/checkpoints.lock.yaml` 指向的目录 | BLOCKED |
| GPT-SoVITS 预训练权重 | 同上 | BLOCKED |
| 用户已验证参考音频 | `config/golden-assets.local.yaml` 的 `base_voice_path` | BLOCKED |
| CUDA 设备 | `nvidia-smi` 可用 | FAIL |
| 三个隔离 Python 3.11 环境 | 见 `config/engines.lock.yaml` | FAIL |

`config/golden-assets.local.yaml`（Git 忽略）schema：

```yaml
schema_version: 1
assets:
  user_verified_primary:
    base_voice_path: D:\absolute\path\to\reference.wav
cases:
  user_verified_zh_ja:
    ref_text_cn: "我已经失去了一切，可我仍然活着。"
    emotion_vector: [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20]
    target_text: "今日は静かな一日だった。"
    seed: 1234
    speed_factor: 1.0
  user_verified_zh_en:
    ref_text_cn: "我已经失去了一切，可我仍然活着。"
    emotion_vector: [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20]
    target_text: "It was a quiet day."
    seed: 1234
    speed_factor: 1.0
```

IndexTTS2 生成、供 GPT-SoVITS 使用的参考音频必须为单声道、非静音、时长在闭区间 `3.0..10.0` 秒；输入 IndexTTS2 的音色素材不受此窗口限制；官方 `voice_01.wav` 仅可作
`developer-smoke`，不计入黄金验收。

## 2. 显存生命周期探针

```powershell
pwsh -NoProfile -File scripts/probe-engine-lifecycle.ps1 `
  -BaseConfig 'D:\TTSsystem\config\acceptance.gpu.local.yaml' `
  -EvidenceDir 'D:\TTSsystem\runtime\developer-gpu\lifecycle' `
  -OutputConfig 'D:\TTSsystem\runtime\developer-gpu\effective.gpu.yaml' -Json
```

探针行为：

1. 以 BaseConfig 所在目录解析全部 path 字段并物化为绝对路径，强制
   `engine_lifecycle: resident`，写 `resident-candidate.yaml`；
2. 独立进程启动 control，要求两个 worker 同时 ready；
3. 预热 Index → 预热 GSV → 连续两轮完整 Index→GSV；
4. 记录 `nvidia-smi` 的 idle / index_peak / gsv_peak / combined_peak；
5. 检查第二轮无模型重新加载日志；
6. 保存 candidate start/doctor/audit/log/stop receipt；
7. 输出固定 schema 的 `lifecycle-decision.json`。

只有明确 CUDA OOM 或 `combined_peak + required_reserve > total_mib` 才回退
`exclusive_process`；其余错误一律 `probe_failed`。

## 3. 正式 GPU 套件

```powershell
$env:VOICE_PIPELINE_RUN_GPU_TESTS='1'
$env:VOICE_PIPELINE_CONFIG='D:\TTSsystem\runtime\developer-gpu\effective.gpu.yaml'
uv run pytest tests/gpu -vv -m "gpu and not gpu_residency"
```

正式套件使用 `-m "gpu and not gpu_residency"`，不会在已启动的最终配置中再次探测。

校验项：

- Index 真实输出可解码、非静音、`3..10` 秒；
- 日语与英语目标音频可解码、非静音、有限数值；
- 两个目标输出 SHA-256 不同；
- manifest 的 reference SHA-256 与 GSV adapter audit log 一致；
- health 明确 `mode=real`；
- 日志无 OOM、traceback 或 fake fallback；
- 动态挑战：随机短句 + 随机 `request_id`，经同一真实 HTTP 链路合成，audit log 含
  动态文本 SHA-256（不写明文）、真实 GSV PID、模型 fingerprint 与 reference SHA-256，
  输出 SHA-256 与两份固定黄金不同。

## 4. 人工听感（不伪造）

每个成功用例目录必须包含：

```text
request.json
reference.wav
target.wav
run-manifest.json
audio-metrics.json
sha256.txt
listening-review.json
```

开发者只能写：

```json
{
  "status": "pending_user_review",
  "reviewer": null,
  "scores": null,
  "blocking_issue": null
}
```

最终分数只能由主智能体展示音频、用户试听反馈后写入。

## 5. 交付状态

- 模型完整：全部 PASS，交付状态 **PASSED**；
- 资产缺失：报告具体缺失路径，交付状态 **BLOCKED**，绝不使用 fake 顶替。
