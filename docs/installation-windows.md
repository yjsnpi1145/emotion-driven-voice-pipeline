# Windows 安装指南

## 1. 前置条件

安装以下工具并重新打开 PowerShell 7：

```powershell
winget install --id Git.Git -e
winget install --id Microsoft.PowerShell -e
winget install --id astral-sh.uv -e
winget install --id CondaForge.Miniforge3 -e --scope user
```

真实模式还需要 NVIDIA 驱动。确认以下命令可运行：

```powershell
git --version
pwsh --version
uv --version
conda --version
nvidia-smi
```

## 2. 先验证 fake 模式

在任意克隆路径的仓库根目录执行：

```powershell
uv sync --frozen --extra dev --python 3.11
uv run voice-pipeline serve --config config/app.fake.yaml
```

访问 <http://127.0.0.1:8765/>。确认系统状态为 ready 后停止进程，再继续真实安装。

## 3. 创建本机配置

```powershell
Copy-Item config/app.example.yaml config/app.public.local.yaml
```

`.local.yaml` 已被 Git 忽略。相对路径以配置文件所在目录为基准；如需导入现有 GSV 整合包
中的训练模型，把其目录加入 `model_library.allowed_import_roots`。

服务必须保持监听 `127.0.0.1`。项目没有公网认证、多用户隔离或远程文件浏览边界，不应把
8765、9871、9880 端口暴露到局域网或互联网。

## 4. 安装三个隔离环境

先阅读根目录 `MODEL_LICENSES.md`。模型安装命令必须显式接受上游条款：

```powershell
pwsh -NoProfile -File scripts/setup-control.ps1
pwsh -NoProfile -File scripts/setup-indextts.ps1 -DownloadModels -AcceptModelLicenses
pwsh -NoProfile -File scripts/setup-gpt-sovits.ps1 -DownloadModels -AcceptModelLicenses
pwsh -NoProfile -File scripts/setup-quality.ps1 -AcceptModelLicenses
```

脚本会：

1. 将固定提交的上游源码克隆到 `external/`；
2. 从受跟踪的 lock 创建 Python 3.11 环境；
3. 下载固定 revision 的模型；
4. 校验 GPT-SoVITS 预训练归档和质量模型哈希；
5. 保留下载资产携带的许可证文件。

已经完整安装模型时，可以不传 `-DownloadModels` 重跑 Index/GSV setup 做环境校验；
IndexTTS2 setup 仍必须传 `-AcceptModelLicenses`。质量模型可以用 `-Offline` 禁止任何下载。

## 5. 显存生命周期

默认公共配置采用 `exclusive_process`：IndexTTS2 与 GPT-SoVITS 不同时常驻，适合显存
紧张的单 GPU 机器。只有 residency probe 证明双驻留仍留有安全余量时才改为 `resident`：

```powershell
pwsh -NoProfile -File scripts/probe-engine-lifecycle.ps1 `
  -BaseConfig config/app.public.local.yaml `
  -EvidenceDir runtime/developer-gpu/lifecycle `
  -OutputConfig runtime/developer-gpu/effective.gpu.yaml `
  -Json
```

## 6. 启动、诊断和停止

```powershell
pwsh -NoProfile -File scripts/start.ps1 `
  -Config config/app.public.local.yaml `
  -PythonExecutable .venv-control/Scripts/python.exe `
  -Json

.venv-control/Scripts/voice-pipeline.exe doctor `
  --server http://127.0.0.1:8765 --json
```

打开 <http://127.0.0.1:8765/>。停止时使用受管 PID 文件：

```powershell
pwsh -NoProfile -File scripts/stop.ps1 `
  -RunFile runtime/run/processes.json `
  -ReceiptPath runtime/run/stop-receipt.json `
  -Json
```

## 7. 配置 LLM 与模型档案

在 WebUI 的 **LLM 设置**中填写 OpenAI 兼容 Base URL、模型和 Key。连接测试成功后保存。

在 **模型管理**中导入有权使用的 GPT `.ckpt` 和 SoVITS `.pth`，然后显式激活。模型会被
复制到 `models/gpt-sovits/` 并按哈希建立档案，不需要修改 GPT-SoVITS 上游源码。

## 8. 常见问题

### `LLM_UNAVAILABLE` / HTTP 400

检查 Base URL 是否已经包含 `/v1`、模型名是否存在，以及服务是否支持
`POST /chat/completions` 与 JSON object response format。

### `LLM_INVALID_RESPONSE`

LLM 返回内容未满足严格分块 schema。查看工作台的 LLM 实时活动窗口；必要时使用更可靠的
模型并提高超时，不要把 API Key 或完整原文粘贴到公开 Issue。

### `REFERENCE_DURATION_OUT_OF_RANGE`

这是 IndexTTS2 输出参考音频的 3–10 秒门禁，不是上传音色素材的限制。系统会自动调整中文
参考文本并重试；也可以在分块编辑器缩短参考文本后只重生成 reference。

### 服务重启后任务仍在，但引擎显示 `stopped_expected`

在 `exclusive_process` 模式下这是正常状态；引擎会在相应 GPU 任务开始时按需启动。

### 端口被占用

先运行 `scripts/stop.ps1`。不要直接删除 `processes.json`；若进程登记确实过期，检查
`runtime/logs` 和 stop receipt 后再处理。
