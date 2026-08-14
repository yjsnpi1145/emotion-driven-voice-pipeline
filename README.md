# Emotion Driven Voice Workbench

面向本地配音制作的跨语言情绪 TTS 工作台。系统先让 OpenAI 兼容 LLM 完成整篇文本的
翻译、分块、中文情绪参考文本与八维情绪向量规划，再用 IndexTTS2 生成中文情绪参考音频，
最后由 GPT-SoVITS 使用选定的自训练模型合成中文、日语、英语、韩语或粤语目标语音。

> 当前版本为 Windows/NVIDIA 单机 Alpha。模型权重不包含在本仓库；仓库只分发控制面、
> WebUI、worker 外壳、安装脚本、配置模板和测试。

## 主要功能

- 整篇文本自动翻译、分块、情绪分析和批量配音；
- 非中文原文自动生成中文 `ref_text_cn`，保证 IndexTTS2 只朗读中文参考；
- 中文原文可以先翻译为目标语言，再进入同一套参考音频与 GSV 流程；
- 每段独立保存不可变 reference/GSV 版本，可试听、切换和恢复冻结参数；
- 分别支持“只重生成参考”“只重生成 GSV”“两者都重生成”；
- 八维情绪向量默认采用 LLM 值，用户可在基准上微调；
- SQLite 持久保存章节、任务、分块、版本指针和输入快照，刷新或重启不会丢失；
- WebUI 支持章节历史删除、阶段进度、LLM 实时活动、深色控制台、模型管理和系统状态；
- 可导入并切换自训练 GPT-SoVITS `.ckpt`/`.pth`，不会强制使用基模；
- 单 GPU 串行调度，支持双引擎常驻或显存不足时的独占进程切换。
- 导演模式可从文章或剧本识别旁白、角色和需配音语句，人工复核后按角色批量合成；
- 导演时间线支持角色拖放、多选分配、语句拆分/合并、角色重命名/拆分/合并；
- 可复用角色预设只保存托管音色 WAV、GPT-SoVITS 模型档案和默认语速；
- 多角色生成先完成全部 IndexTTS2 参考，再按模型档案分组运行 GSV，单句失败不阻断其他语句。

## 工作流程

```text
原文 + 目标语言 + 长音色素材
        │
        ▼
OpenAI 兼容 API
  ├─ 翻译/保真目标文本
  ├─ 分块与停顿
  ├─ 中文情绪参考文本
  └─ 八维情绪向量
        │
        ▼
IndexTTS2（中文参考文本 + 音色素材 + 情绪向量）
        │  生成 3–10 秒中文情绪参考音频
        ▼
GPT-SoVITS（参考音频 + 中文参考文本 + 目标语言文本 + 自训练模型）
        │
        ▼
分块版本历史 ──显式拼接──> final.wav + timeline.json
```

输入给 IndexTTS2 的音色素材可以是较长 WAV；`3–10 秒`约束只作用于 IndexTTS2 输出、
准备交给 GPT-SoVITS 的情绪参考音频。

## 导演模式

WebUI 的 **导演模式** 是独立于普通章节工作台的持久化流程：

1. **导入**：粘贴文章、小说或剧本，选择原文与目标语言；
2. **分析**：LLM 只分析说话人、旁白、舞台说明和语句边界，此阶段不会启动 GPU；
3. **角色复核**：拖动角色到单句或多选语句，也可用下拉框；低置信度语句必须显式确认；
4. **翻译校对**：逐句检查目标语言台词和供 IndexTTS2 使用的中文情绪参考文本；
5. **音色映射与生成**：为每个实际配音角色选择角色预设，确认后才冻结快照并开始推理。

系统默认识别旁白，项目顶部开关可以整体关闭旁白配音，但原文和时间线行不会被删除。
角色预设的基础 WAV 会复制进本地托管库，可使用长音频；它不受 GSV 参考音频的 3–10 秒
窗口限制。分析与翻译按确定性文本块并行调用 LLM，中断后保留已完成块并显示可重试错误。

生成时严格按“全部参考音频 → 按 GPT-SoVITS 模型分组的目标语音 → 原文顺序拼接”执行。
失败只标记对应语句，成功版本保持可用；**继续生成**只补失败/缺失项，**重新拼接**可直接
使用现有成功版本。项目、角色、语句修订、生成快照和编辑事件都保存在 SQLite 中，刷新或
重启服务后仍可继续。

## 架构

```text
Browser / CLI ──HTTP──> FastAPI 控制面 127.0.0.1:8765
                            │
                            ├── SQLite + immutable artifact store
                            ├── OpenAI-compatible /chat/completions
                            └── single-consumer GPU queue
                                  ├── IndexTTS2 worker :9871
                                  └── GPT-SoVITS api_v2 :9880
```

控制面、IndexTTS2 和 GPT-SoVITS 使用三个独立 Python 3.11 环境。浏览器只访问 loopback
控制面，不直接接触模型进程或 LLM API Key。

## 系统要求

### fake 模式

- Windows 10/11；
- PowerShell 7；
- Git；
- [uv](https://docs.astral.sh/uv/)；
- Python 3.11（uv 可以自动安装）。

### 真实推理

除上述要求外还需要：

- NVIDIA CUDA 显卡；
- 可用的 NVIDIA 驱动和 `nvidia-smi`；
- Miniforge/Conda；
- 足够的磁盘空间保存三个环境与模型；
- 一个 OpenAI `/chat/completions` 兼容 API；
- 有权使用的参考音色和 GPT-SoVITS 模型。

本项目目前明确限定 Python `>=3.11,<3.12`。

## 五分钟启动 fake 模式

在克隆后的仓库根目录执行：

```powershell
uv sync --frozen --extra dev --python 3.11
uv run voice-pipeline serve --config config/app.fake.yaml
```

打开 <http://127.0.0.1:8765/>。fake 模式用于检查 WebUI、任务持久化和 API，不会下载或
加载真实模型。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/health
```

## Windows 一键启动真实服务

完成下方真实模型安装后，可以直接双击仓库根目录的 `启动服务.bat`。它会复用现有
PowerShell supervisor 启动控制面和按需模型服务，等待健康检查通过后自动打开 WebUI。
如果服务已经运行，重复双击只会打开页面，不会产生第二套进程。

## 安装真实模型

先阅读 [MODEL_LICENSES.md](MODEL_LICENSES.md)，然后创建本机配置：

```powershell
Copy-Item config/app.example.yaml config/app.public.local.yaml
pwsh -NoProfile -File scripts/setup-control.ps1
pwsh -NoProfile -File scripts/setup-indextts.ps1 -DownloadModels -AcceptModelLicenses
pwsh -NoProfile -File scripts/setup-gpt-sovits.ps1 -DownloadModels -AcceptModelLicenses
pwsh -NoProfile -File scripts/setup-quality.ps1 -AcceptModelLicenses
```

`-AcceptModelLicenses` 表示调用者已经阅读并接受各模型上游条款；本项目不会把上游模型
重新许可为 Apache-2.0。所有下载进入 Git 忽略目录。

启动：

```powershell
pwsh -NoProfile -File scripts/start.ps1 `
  -Config config/app.public.local.yaml `
  -PythonExecutable .venv-control/Scripts/python.exe `
  -Json

.venv-control/Scripts/voice-pipeline.exe doctor `
  --server http://127.0.0.1:8765 --json
```

停止：

```powershell
pwsh -NoProfile -File scripts/stop.ps1 `
  -RunFile runtime/run/processes.json `
  -ReceiptPath runtime/run/stop-receipt.json `
  -Json
```

完整安装、显存生命周期选择和排障见
[Windows 安装指南](docs/installation-windows.md)。

## 配置 LLM

打开 WebUI 的 **LLM 设置**，填写：

- OpenAI 兼容 Base URL；
- 模型名；
- API Key；
- 请求超时、重试次数、参考文本修正次数和导演分析并行请求数。

先点击连接测试，再保存配置。API Key 单独保存在本地 `runtime/state/llm-secret.txt`，不会
由 GET API 或 WebUI 回显。章节原文会发送到用户配置的 OpenAI 兼容 API；详细数据边界见
[PRIVACY.md](PRIVACY.md)。

工作台的 **LLM 实时活动** 窗口会显示请求发出、等待耗时、重试、原始 JSON 响应和解析
结果，但不会展示 Authorization、API Key、请求头或完整 prompt。

## 导入自训练 GPT-SoVITS 模型

1. 打开 WebUI 的 **模型管理**；
2. 选择 GPT `.ckpt` 和 SoVITS `.pth`；
3. 输入配置名称并导入；
4. 显式激活该配置；
5. 新提交的 GSV 任务会冻结模型文件哈希和配置 ID。

已经排队或已经生成的历史版本不会因为切换当前模型而改变。自训练 GPT-SoVITS 模型不应
提交到本仓库；模型分发需要单独确认声音、数据和底模权利。

## 数据与目录

```text
external/                 # 上游源码与模型环境（忽略）
models/                   # 导入的自训练 GSV 模型（忽略）
runtime/
├── state/                # SQLite、LLM 设置与本地密钥（忽略）
├── artifacts/            # 不可变音频与 manifest（忽略）
├── jobs/                 # 任务工作目录（忽略）
├── logs/                 # 运行日志（忽略）
└── run/processes.json    # 受管进程登记（忽略）
```

模型权重不包含在本仓库。音频、数据库、运行日志、`.local.yaml` 和密钥文件都不应进入
Git。发布日志或 Issue 前请先移除本地路径、原文、音频和凭据。

## CLI

```powershell
voice-pipeline serve --config CONFIG_PATH
voice-pipeline doctor --server SERVER_URL --json
voice-pipeline generate-reference --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline generate-gsv --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline synthesize-segment --server SERVER_URL --request REQUEST_JSON --output-dir OUTPUT_DIR --json
voice-pipeline synthesize-chapter --server SERVER_URL --request REQUEST_JSON --output-dir OUTPUT_DIR --json
```

## 开发与测试

```powershell
uv sync --frozen --extra dev --python 3.11
uv run ruff check src tests
uv run mypy src workers
node --check src/voice_pipeline/webui/app.js
uv run pytest -q -m "not gpu and not gpu_residency and not quality_model"
uv build --wheel
```

GPU 黄金验收需要本地模型、真实音色和 CUDA 环境，不在普通 GitHub Actions runner 中运行。

## 许可证

- 本项目原创代码：[Apache License 2.0](LICENSE)；
- 第三方依赖：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；
- 模型与引擎：[MODEL_LICENSES.md](MODEL_LICENSES.md)；
- 隐私：[PRIVACY.md](PRIVACY.md)；
- 安全报告：[SECURITY.md](SECURITY.md)。

IndexTTS2 使用独立的 bilibili Model Use License Agreement，不是 MIT，也不属于本项目的
Apache-2.0 授权范围。
