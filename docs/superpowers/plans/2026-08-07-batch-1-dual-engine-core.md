# 批次 1：双引擎可运行核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Windows 本地机器上交付一个可重复安装、可自动测试的单段配音闭环：固定中文参考文本与合法 8 维情绪向量经 IndexTTS2 生成参考音频，再由 GPT-SoVITS 生成日语或英语目标语音。

**Architecture:** Python 3.11 控制面以单进程 FastAPI 提供内存任务 API，并用唯一的单消费者队列串行化全部 GPU 推理。IndexTTS2 与 GPT-SoVITS 分别运行在独立 Python 环境和独立 loopback 进程中；控制面只通过 HTTP adapter 调用它们，且不导入任何模型包。CLI 只调用控制面，不能绕过队列直连引擎。

**Tech Stack:** Windows PowerShell 7、Python 3.11、uv、FastAPI、Uvicorn、Pydantic v2、HTTPX、Typer、NumPy、SoundFile、PyYAML、pytest、pytest-asyncio、pytest-cov、respx、Ruff、mypy、IndexTTS2、GPT-SoVITS v2、CUDA 12.8。

## Global Constraints

- 工作区固定为 `D:\TTSsystem`，采用 Windows 原生进程和路径；本批次不支持 WSL。
- 控制面、IndexTTS2 worker、GPT-SoVITS 必须使用三个独立 Python 3.11 环境；不得使用系统 Python 3.14。
- IndexTTS2 固定到 `90ca4d608209584bad3a5bd5becc0b80c146e60f`。
- IndexTTS2 模型仓固定到 `IndexTeam/IndexTTS-2@740dcaff396282ffb241903d150ac011cd4b1ede`。
- Index 的 W2V-BERT、MaskGCT semantic codec、CAMPPlus 和 BigVGAN 也必须按 `engines.lock.yaml` 的独立 revision 预下载；真实 worker 只接收显式本地 `aux_paths`，禁止启动时从各仓 `main` 自动下载。
- GPT-SoVITS 固定到 `d523079fc05d9a8028d6085bffe4a2757c32abb6`，批次 1 使用官方 `api_v2.py` 和 v2 权重。
- GPT-SoVITS 官方预训练归档固定到 `XXXXRT/GPT-SoVITS-Pretrained@4fae8ec36d3d0373864e580b5d8acfba8da29630`，`pretrained_models.zip` SHA-256 固定为 `82881ee064a0a49c84160908fd08e4dd0c8946e32567ff8df1ad4dad4c358793`。
- IndexTTS2 使用 `fp16=true`，并关闭 DeepSpeed、flash-attn/accel、CUDA 自定义 kernel 和 `torch.compile`。
- GPT-SoVITS 使用 `batch_size=1`、`streaming_mode=false`、`parallel_infer=false`、`media_type=wav`。
- 情绪向量顺序固定为 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`；必须正好 8 维，单项 `0.0..1.0`，总和 `<=0.8`。
- 应用层和 Index worker 均不得自动归一化、裁剪、乘以隐藏 bias 或改写情绪向量；传入 IndexTTS2 的值必须与请求快照逐项一致。
- IndexTTS2 生成、供 GPT-SoVITS 使用的参考音频必须可解码、非静音、单声道且时长位于闭区间 `3.0..10.0` 秒；输入 IndexTTS2 的音色素材不受此窗口限制；不合格时不得调用 GPT-SoVITS。
- `reference.wav` 与生成它的 `ref_text_cn` 必须绑定；GSV 请求不能另行传入一个可能不一致的 `prompt_text`。
- 所有 GPU 工作必须经过控制面中的一个队列和一个 consumer；Uvicorn worker 数必须固定为 1。
- 所有 HTTP 服务只绑定 `127.0.0.1`；本批次不做公网部署、鉴权或多用户。
- GitHub 开源复用优先：实现任何模块前，先检查上游官方实现和维护活跃、许可证兼容的成熟开源库；能以固定版本/commit 直接依赖的，不自行重写。允许为本项目安全边界自研“薄适配/包装”（Index HTTP worker 外壳、两端 HTTP adapter、原子发布、受管进程生命周期、双引擎编排、单 GPU fail-closed 互斥、reference/manifest 绑定和独立验收 challenge），但内部仍复用成熟库且禁止复制上游源码改名。机器可读取舍写入 `config\open-source-reuse.yaml`，人类说明写入 `docs\batch-1-open-source-reuse.md`。
- 不引入 SQLite、Redis、Celery、分布式队列、多 GPU、缓存、版本历史、LLM、长文本分块、整篇拼接、WebUI 或 SSE。
- 批次 1 只做 WAV 解码、有限数值、非静音和 `3..10` 秒门禁；VAD、ASR/文本对齐、自动改写参考文本和自动重试属于后续批次。
- 单声道、`-50 dBFS`、削波比例和 4/5 试听分数是本批次为可重复验收新增的工程 guardrail，不宣称它们是原设计或上游模型的固有硬限制。
- 生产代码不得根据测试名、黄金文件名或固定文本返回预制 WAV。
- 所有 Windows 子进程必须使用参数数组、绝对路径和 `shell=False`；不得拼接 shell 命令字符串。
- 所有文件写入先落同目录临时文件，再原子发布；失败时不得把半成品暴露成成功产物。
- 对不可变的 job 音频、manifest 和 CLI 交付路径，发布前必须用 `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` 原子保留目标；重复 job 输出或预置 sentinel 必须返回 `OUTPUT_CONFLICT`。`os.replace()` 只能覆盖本次调用自己创建的零字节保留文件，失败时只删除本次拥有的保留文件。`processes.json` 等明确可变的 supervisor 状态文件采用 versioned payload + 原子替换，不适用不可变产物的 O_EXCL 规则。
- 后端模式固定为 `fake`、`external_test`、`real`：`fake` 使用进程内确定性 client；`external_test` 通过 HTTP 连接由验收套件管理的独立假引擎；`real` 通过 HTTP 连接并管理真实模型进程。

---

## 1. 冻结的技术选择

| 项目 | 批次 1 决定 |
|---|---|
| 控制面入口 | FastAPI `127.0.0.1:8765` |
| Index worker | 项目自有薄 FastAPI worker，`127.0.0.1:9871` |
| GPT-SoVITS | 官方 `api_v2.py`，`127.0.0.1:9880` |
| CLI | 只调用控制面 HTTP；服务不可用时直接失败 |
| 任务存储 | 仅内存；控制面重启后任务消失 |
| GPU 串行 | `asyncio.Queue` + 恰好一个 consumer |
| 运行输出 | `D:\TTSsystem\runtime\jobs\{job_id}\` |
| 配置格式 | YAML；本地真实配置以 `.local.yaml` 结尾并被 Git 忽略 |
| 引擎生命周期 | 同时支持 `resident` 与 `exclusive_process`；先探测双驻留，16GB 显存不足时真实配置固定使用 `exclusive_process` |
| 测试后端 | `fake` 用于快速单进程测试；`external_test` 用于跨进程黑盒；两者都不能计作真实 GPU |
| 音频转换 | 批次 1 不重采样；只探测和拒绝不合格音频 |
| 成功确定性 | 固定 seed 并记录，但不要求 GPU 输出逐字节相同 |
| 正式验收 | 开发智能体只提交交付报告；最终 PASS/BLOCKED/FAIL 由当前主智能体独立判定 |

`resident` 表示两个模型进程都保持加载但推理不重叠。`exclusive_process` 表示切换引擎前停止当前模型进程，再启动另一模型进程。两种模式共享同一 API 和测试；不得通过在业务代码中随意 `del model` 或 `torch.cuda.empty_cache()` 掩盖 OOM。

## 2. 交付后的目录结构

```text
D:\TTSsystem\
├── .gitignore
├── .python-version
├── pyproject.toml
├── uv.lock
├── README.md
├── docs\batch-1-open-source-reuse.md
├── config\
│   ├── app.example.yaml
│   ├── acceptance.gpu.local.yaml       # Git 忽略
│   ├── golden-assets.local.yaml        # Git 忽略
│   ├── checkpoints.lock.yaml
│   ├── open-source-reuse.yaml
│   ├── env-locks\
│   │   ├── control-runtime-requirements.lock.txt
│   │   ├── control-runtime-freeze.txt
│   │   ├── index-pip-requirements.lock.txt
│   │   ├── index-pip-freeze.txt
│   │   ├── gsv-conda-explicit.txt
│   │   ├── gsv-pip-requirements.lock.txt
│   │   └── gsv-pip-freeze.txt
│   └── engines.lock.yaml
├── src\voice_pipeline\
│   ├── __init__.py
│   ├── __main__.py
│   ├── main.py
│   ├── cli.py
│   ├── core\
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── inference_tracker.py
│   │   ├── jobs.py
│   │   ├── gpu_queue.py
│   │   └── pipeline.py
│   ├── models\
│   │   ├── schemas.py
│   │   └── ports.py
│   ├── modules\
│   │   ├── audio\atomic_output.py
│   │   ├── audio\wav_probe.py
│   │   ├── indextts\client.py
│   │   ├── indextts\fake.py
│   │   ├── gpt_sovits\client.py
│   │   └── gpt_sovits\fake.py
│   ├── api\
│   │   ├── app.py
│   │   ├── dependencies.py
│   │   └── routes.py
│   └── runtime\
│       ├── audit.py
│       ├── process.py
│       ├── supervisor.py
│       └── fingerprints.py
├── workers\
│   ├── __init__.py
│   └── indextts2\
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── engine.py
│       ├── schemas.py
│       └── requirements.txt
├── scripts\
│   ├── setup-control.ps1
│   ├── setup-indextts.ps1
│   ├── setup-gpt-sovits.ps1
│   ├── lock-engine-assets.ps1
│   ├── start.ps1
│   └── stop.ps1
├── testdata\
│   ├── requests\
│   └── golden\
├── .acceptance\                    # Git 忽略；仅由主智能体验收时创建
├── tests\
│   ├── unit\
│   ├── contract\
│   ├── integration_cpu\
│   ├── process\
│   └── gpu\
├── external\                       # Git 忽略
│   ├── index-tts\
│   └── GPT-SoVITS\
└── runtime\                        # Git 忽略
    ├── jobs\
    ├── logs\
    └── acceptance\
```

## 3. 稳定公共接口

### 3.1 CLI

```powershell
voice-pipeline serve --config CONFIG_PATH
voice-pipeline doctor --server SERVER_URL --json
voice-pipeline generate-reference --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline generate-gsv --server SERVER_URL --request REQUEST_JSON --output OUTPUT_WAV --json
voice-pipeline synthesize-segment --server SERVER_URL --request REQUEST_JSON --output-dir OUTPUT_DIR --json
```

生成类命令的退出码：

```text
0 = success
2 = invalid input/config
3 = control plane or engine unavailable
4 = inference/audio validation failure
5 = queue or inference timeout
```

使用 `--json` 时，stdout 只能有一个 JSON 对象；日志和进度只写 stderr。控制面不可达时必须返回 `CONTROL_PLANE_UNAVAILABLE`，不得回退为直连引擎。

### 3.2 控制面 HTTP

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

三个 POST 均返回 HTTP 202。批次 1 状态只有：

```text
queued -> running -> succeeded | failed
```

`POST /api/v1/jobs/reference` 请求：

```json
{
  "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
  "base_voice_path": "D:\\voices\\character.wav",
  "ref_text_cn": "我已经失去了一切，可我仍然活着。",
  "emotion_vector": [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20],
  "seed": 1234
}
```

`POST /api/v1/jobs/gsv` 请求：

```json
{
  "request_id": "4de7ed6a-00f0-4be6-b916-1f10cf96019e",
  "reference_manifest_path": "D:\\outputs\\reference.reference-manifest.json",
  "target_text": "私はまだ生きている。",
  "target_language": "ja",
  "speed_factor": 1.0,
  "seed": 1234
}
```

`POST /api/v1/jobs/segment` 直接使用 `SegmentSynthesisRequest`。三个接口的 202 envelope 固定为：

```json
{
  "job_id": "ff5f7715-5c82-4b3a-b13b-f15b18db9282",
  "request_id": "735ed096-0334-4f63-b3bb-6d5a3210d2d5",
  "status": "queued",
  "status_url": "/api/v1/jobs/ff5f7715-5c82-4b3a-b13b-f15b18db9282"
}
```

成功后的 `GET /api/v1/jobs/{job_id}` 固定包含 `result`、实际可用的 `audio_urls` 和 `manifest_urls`；失败时 `result` 为 null 并包含稳定的 `error` envelope。两个 manifest 路由只返回该 job 自己的 JSON，缺失时返回 404。`POST /api/v1/control/shutdown` 只接受 loopback 调用，用于 `stop.ps1` 先优雅停止 supervisor 再退出控制面。

`job_id` 与 `request_id` 语义必须分离：

- `job_id` 由控制面针对每次 POST 新建，是唯一执行身份和目录名；
- `request_id` 由调用方提供，只用于端到端关联和日志，不作为文件夹名；
- 重复提交相同 `request_id` 合法，但每次必须得到不同 `job_id`，不得覆盖旧产物；
- API 在入队前创建 `ExecutionContext(job_id, request_id, job_dir)`，所有 service 方法必须显式接收它；
- `job_dir` 恒为 `runtime\jobs\{job_id}`，任何业务代码不得从 `request_id` 再推导输出目录。

### 3.3 Index worker HTTP

```text
GET  /health/live
GET  /health/ready
POST /v1/synthesize
POST /v1/control/stop
```

### 3.4 核心 Python port

```python
class IndexTTSClient(Protocol):
    async def synthesize(
        self,
        request: IndexSynthesisRequest,
        output_path: Path,
    ) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


class GptSoVitsClient(Protocol):
    async def synthesize(
        self,
        request: GsvSynthesisRequest,
        output_path: Path,
    ) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


WorkerName = Literal["indextts", "gpt_sovits"]


class EngineFingerprint(StrictModel):
    schema_version: Literal[1]
    engine: WorkerName
    source_revision: str
    model_revision: str
    engine_lock_sha256: str
    checkpoint_lock_sha256: str
    environment_lock_sha256: str
    runtime_config_sha256: str


class EngineIdentity(StrictModel):
    worker: WorkerName
    pid: int
    create_time: float
    python_executable: Path
    fingerprint: EngineFingerprint


class WorkerHealth(StrictModel):
    state: Literal["ready", "stopped_expected", "starting", "unhealthy", "unknown"]
    pid: int | None
    create_time: float | None
    python_executable: Path
    python_version: str
    source_revision: str
    fingerprint: EngineFingerprint
    preflight_ok: bool
    active_inference: int = 0


class WorkersHealth(StrictModel):
    indextts: WorkerHealth
    gpt_sovits: WorkerHealth


class RuntimeHealth(StrictModel):
    status: Literal["ready", "degraded", "stopping", "stopped"]
    workers: WorkersHealth


class InferenceLease(Protocol):
    async def confirm_completed(self) -> None: ...
    async def confirm_aborted(self) -> None: ...
    async def mark_unknown(self) -> None: ...


class EngineRuntime(Protocol):
    async def start(self) -> None: ...

    async def stop(self, *, deadline: float | None = None) -> None: ...

    async def ensure_engine(self, engine: WorkerName) -> None: ...

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None: ...

    def engine_identity(self, engine: WorkerName) -> EngineIdentity: ...

    def health(self) -> RuntimeHealth: ...

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> InferenceLease: ...
```

`abort_engine()` 的跨实现后置条件统一为“对应 engine 的 active inference 已确认归零”；`ProcessSupervisor` 还必须满足更强条件——对应受管进程树已经退出并原子更新 PID registry；`ExternalEngineRuntime` 则由测试控制端点确认 active request 归零，但不结束验收套件拥有的 fake server。只要远端完成状态不确定，队列在清理确认前绝不能处理下一项。上层服务只依赖这些 Protocol；不得 import worker engine 类或上游模型类。

## 4. 明确非目标

- 不调用 OpenAI 或任何 LLM API。
- 不自动生成或调整参考文本与情绪向量。
- 不实现长文本、分块、角色管理、整篇拼接。
- 不实现 SQLite、不可变 Artifact Version、最近 5 版清理、正式缓存或恢复。
- 不实现 WebUI、SSE、用户权限或公网部署。
- 不实现 VAD、ASR 或文本对齐。
- 不训练 IndexTTS2 或 GPT-SoVITS 模型。
- 不修改或 fork GPT-SoVITS 官方 `api_v2.py`。

---

### Task 1: 初始化仓库、控制面环境和强类型配置

**Files:**
- Create: `D:\TTSsystem\workers\__init__.py`
- Create: `D:\TTSsystem\workers\indextts2\__init__.py`
- Create: `D:\TTSsystem\.gitignore`
- Create: `D:\TTSsystem\.python-version`
- Create: `D:\TTSsystem\pyproject.toml`
- Create: `D:\TTSsystem\README.md`
- Create: `D:\TTSsystem\docs\batch-1-open-source-reuse.md`
- Create: `D:\TTSsystem\config\open-source-reuse.yaml`
- Create: `D:\TTSsystem\config\app.example.yaml`
- Create: `D:\TTSsystem\config\engines.lock.yaml`
- Create: `D:\TTSsystem\src\voice_pipeline\__init__.py`
- Create: `D:\TTSsystem\src\voice_pipeline\core\config.py`
- Create: `D:\TTSsystem\tests\unit\test_config.py`

**Interfaces:**
- Produces: `load_settings(config_path: Path) -> AppSettings`
- Produces: `AppSettings.mode: Literal["fake", "external_test", "real"]`
- Produces: `AppSettings.engine_lifecycle: Literal["resident", "exclusive_process"]`
- Produces: normalized absolute `runtime_dir`, lock files, engine roots and interpreter paths.
- Produces: audited open-source reuse inventory with license and immutable version/commit pins.

- [ ] **Step 1: 初始化 Git 并写配置失败测试**

```powershell
Set-Location 'D:\TTSsystem'
git init
git branch -M main
uv python install 3.11
```

先创建 `config\open-source-reuse.yaml` 与由其生成/校对的 `docs\batch-1-open-source-reuse.md`。YAML schema 固定为 `schema_version: 1` 和非空 `modules` 列表；每项必须含 `module_id`、`need`、`disposition: reuse|thin_wrapper|custom`、至少一个 `candidates`（每个含 GitHub repository、SPDX license、immutable pin）、`selected`、`decision_reason`、`wrapper_boundary`、`lock_reference`。`custom` 必须逐个写 rejected reason；pin 必须能与依赖锁或 engine commit 对上。最低限度直接复用：IndexTTS2 官方 inference、GPT-SoVITS 官方 `api_v2.py`、FastAPI/Uvicorn、HTTPX、Pydantic、Typer、psutil、SoundFile、Hugging Face Hub 与 uv。项目特定薄 worker/adapter/安全 wrapper 可以自研，但不得复制上游源码。若找到更成熟的 GitHub 组件，优先薄封装并补 contract test。

在 `tests\unit\test_config.py` 写入：

```python
from pathlib import Path

import pytest

from voice_pipeline.core.config import load_settings


def test_load_settings_resolves_paths_from_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "配置 目录"
    config_dir.mkdir()
    config = config_dir / "app.local.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server:
  host: 127.0.0.1
  port: 8765
runtime_dir: ../运行 输出
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue:
  max_concurrency: 1
  queue_timeout_seconds: 60
engines:
  indextts:
    base_url: http://127.0.0.1:9871
    python_executable: ../index/.venv/Scripts/python.exe
    repo_dir: ../index
    request_timeout_seconds: 300
  gpt_sovits:
    base_url: http://127.0.0.1:9880
    python_executable: ../gsv/.venv/Scripts/python.exe
    repo_dir: ../gsv
    request_timeout_seconds: 300
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.runtime_dir == (config_dir / "../运行 输出").resolve()
    assert settings.queue.max_concurrency == 1
    assert settings.server.host == "127.0.0.1"


def test_rejects_more_than_one_gpu_consumer(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text(
        """
schema_version: 1
mode: fake
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 8765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 2, queue_timeout_seconds: 60}
engines:
  indextts: {base_url: http://127.0.0.1:9871, python_executable: i.exe, repo_dir: i, request_timeout_seconds: 300}
  gpt_sovits: {base_url: http://127.0.0.1:9880, python_executable: g.exe, repo_dir: g, request_timeout_seconds: 300}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="max_concurrency must be exactly 1"):
        load_settings(config)


def test_accepts_external_http_test_mode(tmp_path: Path) -> None:
    config = tmp_path / "external-test.yaml"
    config.write_text(
        """
schema_version: 1
mode: external_test
engine_lifecycle: resident
server: {host: 127.0.0.1, port: 18765}
runtime_dir: runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue: {max_concurrency: 1, queue_timeout_seconds: 2}
engines:
  indextts:
    {base_url: http://127.0.0.1:19001, python_executable: index-python.exe, repo_dir: index,
     request_timeout_seconds: 2, expected_fingerprint: {challenge: index-123}}
  gpt_sovits:
    {base_url: http://127.0.0.1:19002, python_executable: gsv-python.exe, repo_dir: gsv,
     request_timeout_seconds: 2, expected_fingerprint: {challenge: gsv-456}}
""",
        encoding="utf-8",
    )

    assert load_settings(config).mode == "external_test"
```

- [ ] **Step 2: 运行测试并确认因包不存在而失败**

```powershell
uv run --python 3.11 pytest tests/unit/test_config.py -q
```

Expected: FAIL，包含 `ModuleNotFoundError: No module named 'voice_pipeline'`。

- [ ] **Step 3: 创建项目元数据和依赖**

`pyproject.toml` 至少包含：

```toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "emotion-driven-voice-pipeline"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = [
  "fastapi>=0.115.2,<1",
  "httpx>=0.28,<1",
  "numpy>=2.1,<3",
  "psutil>=6.1,<8",
  "pydantic>=2.10,<3",
  "PyYAML>=6.0.2,<7",
  "soundfile>=0.13,<1",
  "typer>=0.15,<1",
  "uvicorn[standard]>=0.34,<1",
]

[project.optional-dependencies]
dev = [
  "mypy>=1.14,<2",
  "pytest>=8.3,<9",
  "pytest-asyncio>=0.25,<2",
  "pytest-cov>=6,<8",
  "respx>=0.22,<1",
  "ruff>=0.9,<1",
]

[project.scripts]
voice-pipeline = "voice_pipeline.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/voice_pipeline"]

[tool.pytest.ini_options]
addopts = "--strict-config --strict-markers"
asyncio_mode = "auto"
markers = [
  "gpu: requires real CUDA models and checkpoints",
  "gpu_residency: runs the isolated resident-versus-exclusive GPU lifecycle probe",
  "process: starts local subprocesses",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.11"
strict = true
files = ["src/voice_pipeline", "workers"]

[[tool.mypy.overrides]]
module = ["indextts", "indextts.*", "torch", "torch.*"]
ignore_missing_imports = true
```

`.python-version` 必须只有：

```text
3.11
```

`.gitignore` 至少包含：

```gitignore
.venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
__pycache__/
*.py[cod]
.acceptance/
external/
runtime/
config/*.local.yaml
config/*.local.yml
*.key
*.ckpt
*.pth
*.safetensors
```

执行：

```powershell
uv sync --extra dev --python 3.11
```

Expected: 创建根目录 `.venv` 和 `uv.lock`，退出码 0。

- [ ] **Step 4: 实现配置模型和相对路径解析**

`src\voice_pipeline\core\config.py` 的公开形态必须是：

```python
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrency: int = 1
    queue_timeout_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def single_consumer_only(self) -> "QueueSettings":
        if self.max_concurrency != 1:
            raise ValueError("max_concurrency must be exactly 1")
        return self


class EngineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    python_executable: Path
    repo_dir: Path
    request_timeout_seconds: float = Field(gt=0)
    expected_fingerprint: dict[str, str] | None = None


class EnginesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indextts: EngineSettings
    gpt_sovits: EngineSettings


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    mode: Literal["fake", "external_test", "real"]
    engine_lifecycle: Literal["resident", "exclusive_process"]
    server: ServerSettings
    runtime_dir: Path
    engine_lock_path: Path
    checkpoint_lock_path: Path
    queue: QueueSettings
    engines: EnginesSettings


def _resolve_path(base: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (base / value).resolve()


def load_settings(config_path: Path) -> AppSettings:
    resolved_config = config_path.resolve(strict=True)
    raw = yaml.safe_load(resolved_config.read_text(encoding="utf-8"))
    settings = AppSettings.model_validate(raw)
    base = resolved_config.parent
    settings.runtime_dir = _resolve_path(base, settings.runtime_dir)
    settings.engine_lock_path = _resolve_path(base, settings.engine_lock_path)
    settings.checkpoint_lock_path = _resolve_path(base, settings.checkpoint_lock_path)
    for engine in (settings.engines.indextts, settings.engines.gpt_sovits):
        engine.python_executable = _resolve_path(base, engine.python_executable)
        engine.repo_dir = _resolve_path(base, engine.repo_dir)
    return settings
```

`config\engines.lock.yaml` 固定为：

```yaml
schema_version: 1
indextts:
  repository: https://github.com/index-tts/index-tts.git
  revision: 90ca4d608209584bad3a5bd5becc0b80c146e60f
  model_id: IndexTeam/IndexTTS-2
  model_revision: 740dcaff396282ffb241903d150ac011cd4b1ede
  auxiliary_models:
    w2v_bert:
      repository: facebook/w2v-bert-2.0
      revision: da985ba0987f70aaeb84a80f2851cfac8c697a7b
    semantic_codec:
      repository: amphion/MaskGCT
      revision: b9ccc6487b9f486b5b4c22c93010e0b54ddce2e2
    campplus:
      repository: funasr/campplus
      revision: e4b6ede7ce16997aff4ae69fbca1f0175e2afede
    bigvgan:
      repository: nvidia/bigvgan_v2_22khz_80band_256x
      revision: d7b6990ac772ed0ebd93f814912b0027629a7978
gpt_sovits:
  repository: https://github.com/RVC-Boss/GPT-SoVITS.git
  revision: d523079fc05d9a8028d6085bffe4a2757c32abb6
  model_version: v2
  pretrained_repository: XXXXRT/GPT-SoVITS-Pretrained
  pretrained_revision: 4fae8ec36d3d0373864e580b5d8acfba8da29630
  pretrained_archive_sha256: 82881ee064a0a49c84160908fd08e4dd0c8946e32567ff8df1ad4dad4c358793
```

`config\app.example.yaml` 固定为：

```yaml
schema_version: 1
mode: fake
engine_lifecycle: resident
server:
  host: 127.0.0.1
  port: 8765
runtime_dir: ../runtime
engine_lock_path: engines.lock.yaml
checkpoint_lock_path: checkpoints.lock.yaml
queue:
  max_concurrency: 1
  queue_timeout_seconds: 60
engines:
  indextts:
    base_url: http://127.0.0.1:9871
    python_executable: ../external/index-tts/.venv/Scripts/python.exe
    repo_dir: ../external/index-tts
    request_timeout_seconds: 300
  gpt_sovits:
    base_url: http://127.0.0.1:9880
    python_executable: ../external/GPT-SoVITS/.conda/python.exe
    repo_dir: ../external/GPT-SoVITS
    request_timeout_seconds: 300
```

真实绝对路径和模型选择只能出现在被忽略的 `.local.yaml` 中。

三种模式的依赖注入规则必须由 `create_app()` 明确分支，禁止测试特判：

- `fake`：进程内 `FakeIndexTTSClient`、`FakeGptSoVitsClient`、`NoopEngineRuntime`；
- `external_test`：真实 HTTP adapter、外部假引擎 URL、配置中由随机 challenge 生成的非空 `expected_fingerprint`、`ExternalEngineRuntime`（不自行启动模型，只记录健康与 abort 回调）；用于随机端口的三进程黑盒；
- `real`：真实 HTTP adapter 与 `ProcessSupervisor`，由控制面管理真实 worker。

App-level validator 要求 `external_test` 的两个 challenge dict 均非空；它们不是公共 fingerprint schema。dependency builder 对 `engine + challenge` 做 canonical SHA-256，确定性生成字段完整的 `EngineFingerprint`，fake server 与 adapter 接收同一对象。`real` 禁止从 app YAML 信任 fingerprint，必须从 locks 和本地文件重新计算；`fake` 使用代码固定、同样字段完整的 fake fingerprint。

- [ ] **Step 5: 验证配置测试、格式和类型**

```powershell
uv run pytest tests/unit/test_config.py -q
uv run ruff format .
uv run ruff check .
uv run mypy src/voice_pipeline
```

Expected: 全部退出码 0。

- [ ] **Step 6: 提交**

```powershell
git add .gitignore .python-version pyproject.toml uv.lock README.md docs/batch-1-open-source-reuse.md config src/voice_pipeline tests/unit/test_config.py
git commit -m "build: scaffold batch one control plane"
```

---

### Task 2: 定义领域 Schema、稳定错误和独立 WAV 探测器

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\core\errors.py`
- Create: `D:\TTSsystem\src\voice_pipeline\models\schemas.py`
- Create: `D:\TTSsystem\src\voice_pipeline\models\ports.py`
- Create: `D:\TTSsystem\src\voice_pipeline\modules\audio\atomic_output.py`
- Create: `D:\TTSsystem\src\voice_pipeline\modules\audio\wav_probe.py`
- Create: `D:\TTSsystem\tests\unit\test_schemas.py`
- Create: `D:\TTSsystem\tests\unit\test_atomic_output.py`
- Create: `D:\TTSsystem\tests\unit\test_wav_probe.py`

**Interfaces:**
- Produces: shared `EmotionVector`/`NonBlankText`, `EngineFingerprint`, `ExecutionContext`, `ReferenceJobRequest`, `GsvJobRequest`, `IndexSynthesisRequest`, `ReferenceBinding`, `GsvSynthesisRequest`, `SegmentSynthesisRequest`, `ReferenceSynthesisResult`, `GsvSynthesisResult`, `SegmentSynthesisResult`, `AudioResult`.
- Produces: `PipelineError(code, stage, message, retryable, details)`.
- Produces: `sha256_file(path: Path) -> str` and `probe_wav(path: Path, *, require_reference_window: bool) -> AudioResult`.
- Produces: `reserve_output_path(path: Path) -> OutputReservation` with exclusive ownership and rollback.
- Produces: async `IndexTTSClient`, `GptSoVitsClient` and `EngineRuntime` Protocols.

- [ ] **Step 1: 写情绪向量和绑定关系的失败测试**

```python
import pytest
from pydantic import ValidationError

from voice_pipeline.models.schemas import IndexSynthesisRequest


VALID = [0.0, 0.02, 0.28, 0.03, 0.0, 0.27, 0.0, 0.20]


@pytest.mark.parametrize(
    "vector",
    [
        [0.1] * 7,
        [0.1] * 9,
        [-0.1, 0, 0, 0, 0, 0, 0, 0],
        [1.1, 0, 0, 0, 0, 0, 0, 0],
        [0.11] * 8,
    ],
)
def test_rejects_invalid_emotion_vectors(vector: list[float], tmp_path) -> None:
    with pytest.raises(ValidationError):
        IndexSynthesisRequest(
            request_id="d613a571-1d69-4f6e-a1b7-3222f61657b8",
            text="我已经失去了一切，可我仍然活着。",
            speaker_audio_path=(tmp_path / "voice.wav").resolve(),
            emotion_vector=vector,
            seed=1234,
        )


def test_preserves_vector_exactly(tmp_path) -> None:
    request = IndexSynthesisRequest(
        request_id="d613a571-1d69-4f6e-a1b7-3222f61657b8",
        text="我已经失去了一切，可我仍然活着。",
        speaker_audio_path=(tmp_path / "voice.wav").resolve(),
        emotion_vector=VALID,
        seed=1234,
    )
    assert list(request.emotion_vector) == VALID
```

`tests\unit\test_schemas.py` 还必须把同一组非法向量分别送入 `SegmentSynthesisRequest`、`ReferenceJobRequest` 和从 JSON 重建的 `ReferenceBinding`，断言全部在模型构造阶段抛出 `ValidationError`；不得只测内部 `IndexSynthesisRequest`。API 集成测试另断言非法公共请求返回 HTTP 422、未创建 job record/job 目录且两个 engine 调用数均为 0。

同一文件还要把 `""`、纯空格、`\r\n\t` 分别送入所有公共/内部文本字段（`ref_text_cn`、`target_text`、Index/GSV `text`、manifest `ReferenceBinding.ref_text_cn`），断言共享 `NonBlankText` 在模型构造时拒绝；合法文本统一保存 strip 后值。不得让空白文本延迟到 job 目录创建或 engine 调用后才失败。

- [ ] **Step 2: 写 WAV 门禁失败测试**

```python
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from voice_pipeline.core.errors import PipelineError
from voice_pipeline.modules.audio.wav_probe import probe_wav


def write_tone(path: Path, seconds: float, amplitude: float = 0.2) -> None:
    sample_rate = 22050
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    data = amplitude * np.sin(2 * np.pi * 220 * t)
    sf.write(path, data.astype(np.float32), sample_rate, subtype="PCM_16")


def test_reference_must_be_between_three_and_nine_seconds(tmp_path: Path) -> None:
    short = tmp_path / "short.wav"
    write_tone(short, 2.9)
    with pytest.raises(PipelineError, match="REFERENCE_DURATION_OUT_OF_RANGE"):
        probe_wav(short, require_reference_window=True)


def test_rejects_silent_wav(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    sf.write(silent, np.zeros(22050 * 4, dtype=np.float32), 22050)
    with pytest.raises(PipelineError, match="AUDIO_SILENT"):
        probe_wav(silent, require_reference_window=True)
```

- [ ] **Step 3: 写输出保留和 sentinel 失败测试**

`tests\unit\test_atomic_output.py` 必须覆盖：

```python
def test_reservation_rejects_existing_target_without_modifying_it(tmp_path: Path) -> None:
    target = tmp_path / "target.wav"
    target.write_bytes(b"sentinel")
    before = sha256_file(target)

    with pytest.raises(PipelineError, match="OUTPUT_CONFLICT"):
        reserve_output_path(target)

    assert target.read_bytes() == b"sentinel"
    assert sha256_file(target) == before


def test_failed_publish_removes_only_owned_empty_reservation(tmp_path: Path) -> None:
    target = tmp_path / "target.wav"
    reservation = reserve_output_path(target)
    reservation.rollback()
    assert not target.exists()
```

`reserve_output_path()` 必须以 `os.O_CREAT | os.O_EXCL | os.O_WRONLY` 创建零字节目标文件并记录 ownership；`publish(partial_path)` 只允许 `os.replace(partial_path, target)` 覆盖该对象拥有的 reservation；`rollback()` 只删除仍是本对象所拥有的零字节 reservation。目标预先存在、重复 publish 或 ownership 丢失均返回 `OUTPUT_CONFLICT`，不能覆盖或删除别人的文件。

- [ ] **Step 4: 运行并确认失败**

```powershell
uv run pytest tests/unit/test_schemas.py tests/unit/test_wav_probe.py tests/unit/test_atomic_output.py -q
```

Expected: FAIL，缺少 `models.schemas`、`wav_probe` 和 `atomic_output`。

- [ ] **Step 5: 实现强类型 Schema，结构上禁止 prompt_text 错配**

`models\schemas.py` 必须采用以下核心定义：

```python
from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

RawEmotionVector = tuple[float, float, float, float, float, float, float, float]
LanguageCode = Literal["zh", "ja", "en", "ko", "yue"]
WorkerName = Literal["indextts", "gpt_sovits"]


def _validate_emotion_vector(value: RawEmotionVector) -> RawEmotionVector:
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in value):
        raise ValueError("emotion vector values must be finite and within 0.0..1.0")
    if math.fsum(value) > 0.8 + 1e-9:
        raise ValueError("emotion vector sum must be <= 0.8")
    return value


EmotionVector = Annotated[RawEmotionVector, AfterValidator(_validate_emotion_vector)]


def _validate_non_blank_text(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("text must not be blank")
    return stripped


NonBlankText = Annotated[str, AfterValidator(_validate_non_blank_text)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionContext(StrictModel):
    job_id: UUID
    request_id: UUID
    job_dir: Path


class AudioResult(StrictModel):
    path: Path
    duration_seconds: float = Field(gt=0)
    sample_rate: int = Field(ge=8000, le=192000)
    channels: int = Field(ge=1, le=2)
    frames: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rms_dbfs: float
    peak_dbfs: float


class EngineFingerprint(StrictModel):
    schema_version: Literal[1]
    engine: WorkerName
    source_revision: str
    model_revision: str
    engine_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EngineIdentity(StrictModel):
    worker: WorkerName
    pid: int
    create_time: float
    python_executable: Path
    fingerprint: EngineFingerprint


class WorkerHealth(StrictModel):
    state: Literal["ready", "stopped_expected", "starting", "unhealthy", "unknown"]
    pid: int | None
    create_time: float | None
    python_executable: Path
    python_version: str
    source_revision: str
    fingerprint: EngineFingerprint
    preflight_ok: bool
    active_inference: int = Field(default=0, ge=0, le=1)


class WorkersHealth(StrictModel):
    indextts: WorkerHealth
    gpt_sovits: WorkerHealth


class RuntimeHealth(StrictModel):
    status: Literal["ready", "degraded", "stopping", "stopped"]
    workers: WorkersHealth


class IndexSynthesisRequest(StrictModel):
    request_id: UUID
    text: NonBlankText
    speaker_audio_path: Path
    emotion_vector: EmotionVector
    seed: int
    use_random: Literal[False] = False


class ReferenceBinding(StrictModel):
    audio: AudioResult
    ref_text_cn: NonBlankText
    emotion_vector: EmotionVector
    base_voice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_fingerprint: EngineFingerprint


class GsvSynthesisRequest(StrictModel):
    request_id: UUID
    reference: ReferenceBinding
    text: NonBlankText
    text_lang: LanguageCode
    prompt_lang: Literal["zh"] = "zh"
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: int = -1


class SegmentSynthesisRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    ref_text_cn: NonBlankText
    emotion_vector: EmotionVector
    target_text: NonBlankText
    target_language: LanguageCode
    seed: int = 1234
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)


class ReferenceJobRequest(StrictModel):
    request_id: UUID
    base_voice_path: Path
    ref_text_cn: NonBlankText
    emotion_vector: EmotionVector
    seed: int = 1234


class GsvJobRequest(StrictModel):
    request_id: UUID
    reference_manifest_path: Path
    target_text: NonBlankText
    target_language: LanguageCode
    speed_factor: float = Field(default=1.0, ge=0.5, le=2.0)
    seed: int = 1234


class ReferenceSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    reference: ReferenceBinding
    manifest_path: Path


class GsvSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    target: AudioResult
    reference_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_path: Path


class SegmentSynthesisResult(StrictModel):
    job_id: UUID
    request_id: UUID
    reference: AudioResult
    target: AudioResult
    reference_binding: ReferenceBinding
    reference_manifest_path: Path
    run_manifest_path: Path
```

注意：`GsvSynthesisRequest` 只有 `reference: ReferenceBinding`，不提供独立 `prompt_text` 字段。GSV adapter 必须从 `reference.ref_text_cn` 构造官方 `prompt_text`。

`generate-reference` 的 manifest 必须序列化完整 `ReferenceBinding`。`GsvJobRequest.reference_manifest_path` 指向该 manifest；服务读取后重新计算 WAV SHA-256，并要求它与 manifest 的 `audio.content_sha256` 相同，从而让独立 GSV 生成也不能错配参考文本。`ExecutionContext.job_id` 必须进入所有 result 和 manifest；`ExecutionContext.request_id` 必须与请求体的 `request_id` 相等，否则在任何文件创建前返回 `INVALID_INPUT`。

- [ ] **Step 6: 实现错误模型、输出保留和独立音频探测**

`core\errors.py` 必须提供：

```python
from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONTROL_PLANE_UNAVAILABLE = "CONTROL_PLANE_UNAVAILABLE"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INDEX_ENGINE_ERROR = "INDEX_ENGINE_ERROR"
    INDEX_TIMEOUT = "INDEX_TIMEOUT"
    GSV_ENGINE_ERROR = "GSV_ENGINE_ERROR"
    GSV_TIMEOUT = "GSV_TIMEOUT"
    INVALID_AUDIO = "INVALID_AUDIO"
    AUDIO_SILENT = "AUDIO_SILENT"
    REFERENCE_DURATION_OUT_OF_RANGE = "REFERENCE_DURATION_OUT_OF_RANGE"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"


class PipelineError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        stage: str,
        message: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
        requires_engine_abort: bool = False,
        poison_queue: bool = False,
    ) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.retryable = retryable
        self.details = details or {}
        self.requires_engine_abort = requires_engine_abort
        self.poison_queue = poison_queue

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
```

`requires_engine_abort` 与 `poison_queue` 是控制面内部安全标志，不进入 `as_dict()` 公共错误 envelope。HTTP adapter 在请求已送出后发生 read timeout、连接 reset、截断流、响应读取异常，或在 adapter await 期间收到 cancellation 时，必须令 `requires_engine_abort=True`；连接建立前失败或已收到完整 HTTP 错误响应时为 false。只有 runtime 无法确认 active inference 已归零时才设置 `poison_queue=True`。

`modules\audio\wav_probe.py` 必须由 SoundFile 独立读取真实文件，计算 SHA-256、RMS 和 peak；不得信任 adapter 返回的 metadata。判定值固定为：

```text
reference duration: 3.0 <= seconds <= 10.0
target duration: seconds > 0.1
channels: exactly 1
all samples finite
RMS: greater than -50 dBFS
```

RMS 计算使用：

```python
rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
rms_dbfs = 20.0 * math.log10(max(rms, 1e-12))
```

损坏、NaN/Inf、多声道、静音和越界时抛出带稳定 `ErrorCode` 的 `PipelineError`。

所有 fake/real adapter、worker 和 CLI 发布文件都必须复用 `atomic_output.py` 的同一 reservation 语义；不得各自用裸 `os.replace()` 或“先检查存在、后写入”的 TOCTOU 实现。

- [ ] **Step 7: 定义 adapter Protocol**

`models\ports.py`：

```python
from pathlib import Path
from typing import Protocol
from uuid import UUID

from voice_pipeline.models.schemas import (
    AudioResult,
    EngineFingerprint,
    EngineIdentity,
    GsvSynthesisRequest,
    IndexSynthesisRequest,
    RuntimeHealth,
    WorkerName,
)


class IndexTTSClient(Protocol):
    async def synthesize(
        self, request: IndexSynthesisRequest, output_path: Path
    ) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


class GptSoVitsClient(Protocol):
    async def synthesize(self, request: GsvSynthesisRequest, output_path: Path) -> AudioResult: ...

    def fingerprint(self) -> EngineFingerprint: ...


class InferenceLease(Protocol):
    async def confirm_completed(self) -> None: ...
    async def confirm_aborted(self) -> None: ...
    async def mark_unknown(self) -> None: ...


class EngineRuntime(Protocol):
    async def start(self) -> None: ...

    async def stop(self, *, deadline: float | None = None) -> None: ...

    async def ensure_engine(self, engine: WorkerName) -> None: ...

    async def abort_engine(
        self,
        engine: WorkerName,
        *,
        reason: str,
        deadline: float | None = None,
    ) -> None: ...

    def engine_identity(self, engine: WorkerName) -> EngineIdentity: ...

    def health(self) -> RuntimeHealth: ...

    async def begin_inference(self, engine: WorkerName, *, job_id: UUID) -> InferenceLease: ...
```

- [ ] **Step 8: 运行测试并提交**

```powershell
uv run pytest tests/unit/test_schemas.py tests/unit/test_wav_probe.py tests/unit/test_atomic_output.py -q
uv run ruff check .
uv run mypy src/voice_pipeline
git add src/voice_pipeline tests/unit
git commit -m "feat: define synthesis contracts and wav gates"
```

Expected: 全部退出码 0。

---

### Task 3: 实现确定性假引擎与单段应用服务

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\modules\indextts\fake.py`
- Create: `D:\TTSsystem\src\voice_pipeline\modules\gpt_sovits\fake.py`
- Create: `D:\TTSsystem\src\voice_pipeline\core\pipeline.py`
- Create: `D:\TTSsystem\src\voice_pipeline\core\inference_tracker.py`
- Create: `D:\TTSsystem\src\voice_pipeline\runtime\audit.py`
- Create: `D:\TTSsystem\tests\unit\test_pipeline.py`
- Create: `D:\TTSsystem\tests\unit\test_inference_tracker.py`
- Create: `D:\TTSsystem\tests\unit\test_fake_clients.py`

**Interfaces:**
- Consumes: Task 2 Protocols and schemas.
- Produces: `SynthesisService.generate_reference()`, `generate_gsv()`, `synthesize_segment()`.
- Produces: fake clients that generate request-dependent valid WAV and support delay/failure injection.
- Produces: `NoopEngineRuntime` for fake mode; the service always calls the runtime port before an adapter.
- Produces: `EngineAuditWriter` with per-control-instance append-only JSONL.
- Produces: `InferenceTracker`/`InferenceLease`; it is the control plane's authoritative begin/confirmed-complete/confirmed-abort/unknown source used by runtime health and shutdown.

- [ ] **Step 1: 写严格顺序和绑定测试**

```python
from pathlib import Path

import pytest

from voice_pipeline.core.pipeline import SynthesisService
from voice_pipeline.models.schemas import ExecutionContext, SegmentSynthesisRequest


@pytest.mark.asyncio
async def test_segment_runs_index_then_gsv_with_bound_prompt(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    audit_events: list[dict[str, object]] = []
    index = RecordingIndexClient(calls, duration_seconds=4.0)
    gsv = RecordingGsvClient(calls)
    runtime = RecordingEngineRuntime(calls)
    service = SynthesisService(
        index=index,
        gsv=gsv,
        runtime=runtime,
        audit=RecordingAuditWriter(audit_events),
    )
    request = SegmentSynthesisRequest(
        request_id="cf2deece-f4e8-4114-954b-bfc907730e01",
        base_voice_path=(tmp_path / "音色 voice.wav").resolve(),
        ref_text_cn="我已经失去了一切，可我仍然活着。",
        emotion_vector=[0, 0.02, 0.28, 0.03, 0, 0.27, 0, 0.20],
        target_text="私はすべてを失った。それでも、まだ生きている。",
        target_language="ja",
        seed=1234,
    )
    write_tone(request.base_voice_path, seconds=5.0)
    context = ExecutionContext(
        job_id="aaaaaaaa-0000-4000-8000-000000000001",
        request_id=request.request_id,
        job_dir=tmp_path / "jobs" / "aaaaaaaa-0000-4000-8000-000000000001",
    )

    result = await service.synthesize_segment(context, request)

    assert [name for name, _ in calls] == [
        "ensure:indextts",
        "index",
        "ensure:gpt_sovits",
        "gsv",
    ]
    gsv_request = calls[3][1]
    assert gsv_request.reference.ref_text_cn == request.ref_text_cn
    assert gsv_request.reference.audio.path == result.reference.path
    assert result.job_id == context.job_id
    assert result.reference.path.parent == context.job_dir
    assert [event["engine"] for event in audit_events if event["event"] == "inference_started"] == [
        "indextts",
        "gpt_sovits",
    ]


@pytest.mark.asyncio
async def test_invalid_reference_never_calls_gsv(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []
    audit_events: list[dict[str, object]] = []
    service = SynthesisService(
        index=RecordingIndexClient(calls, duration_seconds=2.0),
        gsv=RecordingGsvClient(calls),
        runtime=RecordingEngineRuntime(calls),
        audit=RecordingAuditWriter(audit_events),
    )
    request = make_request(tmp_path)
    write_tone(request.base_voice_path, seconds=5.0)
    context = make_context(tmp_path, request.request_id)
    with pytest.raises(Exception, match="REFERENCE_DURATION_OUT_OF_RANGE"):
        await service.synthesize_segment(context, request)
    assert [name for name, _ in calls] == ["ensure:indextts", "index"]
```

本文件顶部或 `tests\unit\conftest.py` 必须把 `RecordingIndexClient`、`RecordingGsvClient`、`RecordingEngineRuntime`、`RecordingInferenceLease`、`RecordingAuditWriter`、`make_request`、`make_context`、`write_tone` 定义为可运行 fixture/helper；计划中的伪代码名不能留作未定义符号。

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/unit/test_pipeline.py tests/unit/test_inference_tracker.py tests/unit/test_fake_clients.py -q
```

Expected: FAIL，缺少 fake clients 和 `SynthesisService`。

- [ ] **Step 3: 实现确定性假 WAV**

假引擎的音调频率必须从规范化请求 JSON 的 SHA-256 推导，而不是固定音频：

```python
def frequency_for(payload: dict[str, object], *, base: int) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base + int(hashlib.sha256(encoded).hexdigest()[:4], 16) % 400
```

Index fake 生成 4 秒、22050 Hz、单声道 PCM16 WAV；GSV fake 生成 1.5 秒、32000 Hz、单声道 PCM16 WAV。二者都：

- 先用 `reserve_output_path(output_path)` 独占最终目标；
- 再写由 UUID 生成且与目标同目录的 partial；
- 成功探测后调用 reservation 的 `publish(partial)`；
- 任意失败调用 `rollback()`，只清理本次 reservation 与 partial；
- 支持构造参数 `delay_seconds`；
- 支持构造参数 `failure: PipelineError | None`；
- 返回由 `probe_wav()` 对真实输出文件计算的 `AudioResult`。

`NoopEngineRuntime.start()/stop(deadline=...)` 幂等，`ensure_engine()` 只接受两个已知 `WorkerName` 并立即返回；`abort_engine()` 同样只验证 literal、记录原因并立即返回；`engine_identity()` 返回强类型 `EngineIdentity`，fake fingerprint 也填满固定 `EngineFingerprint` 字段（hash 字段用 fake revision 的真实 SHA-256，而不是另造 schema）；`health()` 返回与真实 runtime 同一个 `RuntimeHealth` schema。三种 runtime 都持有同一个 `InferenceTracker` 并由 `begin_inference()` 发放 lease。应用服务在创建任何 job 目录前，必须要求 context/request 的 `request_id` 相同，并要求所有输入路径为存在的绝对普通文件；相对路径、目录、缺失文件和符号链接越界返回 `INVALID_INPUT`。

`EngineAuditWriter` 每次控制面启动生成 UUID `instance_id`，写入 `runtime\logs\{instance_id}\engine-audit.jsonl`。每行 schema 固定为：

```text
schema_version, timestamp_utc, instance_id, job_id, request_id,
engine, event(ready|inference_started|inference_completed|aborted|stopped),
engine_pid, engine_create_time, target_text_sha256_or_null,
reference_sha256_or_null, engine_fingerprint, monotonic_time
```

写入由控制面单进程加锁、append+flush；不能依赖官方 GSV 自报。每次 `ensure_engine()` 后，service 从 `runtime.engine_identity()` 取 PID/create time；GSV `inference_started` 必须记录目标文本 SHA-256 和 reference SHA-256。manifest 写入同一个 `instance_id`/job interval。测试用 `RecordingAuditWriter`，真实/fake/external 都走同一调用点。

- [ ] **Step 4: 实现应用服务**

`SynthesisService.synthesize_segment()` 必须按以下结构实现：

```python
async def synthesize_segment(
    self,
    context: ExecutionContext,
    request: SegmentSynthesisRequest,
) -> SegmentSynthesisResult:
    self._validate_context(context, request.request_id)
    self._validate_inputs(request)
    job_dir = context.job_dir
    job_dir.mkdir(parents=True, exist_ok=False)
    reference_path = job_dir / "reference.wav"
    target_path = job_dir / "target.wav"

    index_request = IndexSynthesisRequest(
        request_id=request.request_id,
        text=request.ref_text_cn,
        speaker_audio_path=request.base_voice_path,
        emotion_vector=request.emotion_vector,
        seed=request.seed,
        use_random=False,
    )
    await self._runtime.ensure_engine("indextts")
    reference_audio = await self._invoke_engine(
        "indextts",
        lambda: self._index.synthesize(index_request, reference_path),
        job_id=context.job_id,
    )
    reference_audio = probe_wav(reference_audio.path, require_reference_window=True)
    binding = ReferenceBinding(
        audio=reference_audio,
        ref_text_cn=request.ref_text_cn,
        emotion_vector=request.emotion_vector,
        base_voice_sha256=sha256_file(request.base_voice_path),
        engine_fingerprint=self._index.fingerprint(),
    )
    reference_manifest_path = job_dir / "reference-manifest.json"
    atomic_write_json(
        reference_manifest_path,
        build_reference_manifest(context, request, binding),
    )
    gsv_request = GsvSynthesisRequest(
        request_id=request.request_id,
        reference=binding,
        text=request.target_text,
        text_lang=request.target_language,
        speed_factor=request.speed_factor,
        seed=request.seed,
    )
    await self._runtime.ensure_engine("gpt_sovits")
    target_audio = await self._invoke_engine(
        "gpt_sovits",
        lambda: self._gsv.synthesize(gsv_request, target_path),
        job_id=context.job_id,
    )
    target_audio = probe_wav(target_audio.path, require_reference_window=False)
    result = SegmentSynthesisResult(
        job_id=context.job_id,
        request_id=request.request_id,
        reference=reference_audio,
        target=target_audio,
        reference_binding=binding,
        reference_manifest_path=reference_manifest_path,
        run_manifest_path=job_dir / "run-manifest.json",
    )
    atomic_write_json(
        result.run_manifest_path,
        build_run_manifest(context, request, result),
    )
    return result
```

`synthesize_segment()` 必须在 GSV 调用前发布 reference manifest，所以 GSV 失败仍保留完整、可复用的参考产物；run manifest 只有整段成功后才发布。`generate_reference(context, request)` 只调用 Index，并原子写出包含完整 `ReferenceBinding` 的 `reference-manifest.json`。`generate_gsv(context, request)` 只调用 GSV：它读取 reference manifest、重新探测音频并核对 SHA-256，绝不调用 Index；调用前后还必须断言 reference WAV 和 reference manifest 的 SHA-256 均未变化。所有 job 路径由 API 生成的 `job_id` 创建，用户请求不得包含 worker 输出路径。相同 `request_id` 的两次调用必须创建两个不同 job 目录。

`_validate_inputs()` 必须在 `job_dir.mkdir()` 前完成所有当前操作需要的验证：reference/segment 的 `base_voice_path`，gsv 的 `reference_manifest_path`，以及 manifest 内绑定的 reference WAV 都必须是存在的绝对普通文件、不得是目录或越过允许根目录的 symlink；所有文本已经由共享 schema 判定非空，所有情绪向量已经由共享 `EmotionVector` 判定有限、逐项 `0..1` 且总和 `<=0.8`。任一步失败时不得创建 job 目录。

`_invoke_engine()` 是唯一 adapter 调用包装器。紧邻 adapter 前先 `lease = await runtime.begin_inference(engine, job_id=context.job_id)`，此时 `health().workers.<engine>.active_inference` 必须为 1。正常返回或完成状态已确认的完整 HTTP 错误调用 `lease.confirm_completed()`；任何 `PipelineError.requires_engine_abort` 先执行 `await runtime.abort_engine()`，确认后调用 `lease.confirm_aborted()` 才重新抛原错；adapter await 期间的 `asyncio.CancelledError` 也要在 `asyncio.shield()` 中走同一 abort/confirm 流程。若 abort 自身失败或无法确认 active inference 归零，调用 `lease.mark_unknown()`（active 保持 1、worker state 变 unknown），再抛 `ENGINE_UNAVAILABLE` 且 `poison_queue=True`。普通已确认 HTTP 4xx/5xx 或本地输入/音频错误不触发 abort。unit/contract tests 必须分别覆盖 tracker 的 begin/三种 terminal transition，以及 request timeout、read timeout、post-dispatch reset、截断流、cancellation、connect-before-dispatch failure 和完整 HTTP 500。

`InferenceTracker` 每个 engine 同时只允许一个 lease；重复 begin 是内部错误。它用 `asyncio.Lock` 保护状态，向 `EngineRuntime.health()` 提供唯一的 `active_inference` 来源；不得从官方 GSV `/docs` 猜 active。shutdown 在 queue active 但 tracker 状态异常时仍要 fail-safe 地 abort 所有非 `stopped_expected` worker，不能因计数为 0 漏杀。

伪代码中每个 adapter 调用的紧邻前后必须补齐 audit：`ensure_engine()` 返回后取 `engine_identity()` 并写 `inference_started`；成功/失败/abort 后写对应 terminal event。audit 写入失败属于 `ENGINE_UNAVAILABLE`，不得在缺审计的情况下继续真实推理。

`atomic_write_json(path, payload)` 定义在 `modules\audio\atomic_output.py`：规范化 UTF-8 JSON 写入同目录 partial，fsync 后通过 `reserve_output_path()` 发布，不覆盖已有文件。

`build_reference_manifest()` 和 `build_run_manifest()` 至少记录：

```text
schema_version
job_id
request_id
request_snapshot
seed
effective_inference_parameters
engine_and_checkpoint_fingerprints
output_audio_metrics_and_sha256
created_at_utc
```

远端完成状态不确定时的强制顺序是：adapter 抛出带 `requires_engine_abort=True` 的错误（`details.owned_temporary_paths` 记录本次 UUID 临时路径）→ service `await abort_engine()` → runtime 确认 active inference 归零（真实 supervisor 还确认进程树退出并更新 PID registry）→ service 仅清理这些经过 job-root 校验的 owned 临时路径/reservation → service 才重新抛错 → queue 才结束当前项。`abort_engine()` 自己失败时返回 `ENGINE_UNAVAILABLE` 且 queue 进入 poisoned、控制面 degraded，绝不能假装旧 GPU 推理已停止。

- [ ] **Step 5: 验证、提交**

```powershell
uv run pytest tests/unit/test_pipeline.py tests/unit/test_inference_tracker.py tests/unit/test_fake_clients.py -q
uv run ruff check .
uv run mypy src/voice_pipeline
git add src/voice_pipeline tests/unit
git commit -m "feat: add isolated single segment synthesis service"
```

Expected: 全部退出码 0。

---

### Task 4: 实现内存任务注册表和唯一单消费者 GPU 队列

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\core\jobs.py`
- Create: `D:\TTSsystem\src\voice_pipeline\core\gpu_queue.py`
- Create: `D:\TTSsystem\tests\unit\test_jobs.py`
- Create: `D:\TTSsystem\tests\unit\test_gpu_queue.py`

**Interfaces:**
- Produces: `InMemoryJobRegistry.create/get/mark_running/mark_succeeded/mark_failed`.
- Produces: `SerialGpuQueue.start/run/poison/resume_after_verified_recovery/stop/stats`.
- Guarantees: every submitted GPU command executes inside one consumer; ordinary failures do not wedge the queue；无法确认远端清理时 queue fail-closed。

- [ ] **Step 1: 写并发和异常释放失败测试**

```python
import asyncio

import pytest

from voice_pipeline.core.gpu_queue import SerialGpuQueue


@pytest.mark.asyncio
async def test_queue_never_runs_two_gpu_calls_at_once() -> None:
    active = 0
    max_active = 0

    async def work(value: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return value

    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    try:
        results = await asyncio.gather(*(queue.run(lambda i=i: work(i)) for i in range(6)))
    finally:
        await queue.stop()

    assert results == list(range(6))
    assert max_active == 1
    assert queue.stats().max_active_observed == 1


@pytest.mark.asyncio
async def test_failure_releases_queue_for_next_job() -> None:
    async def failing_work() -> None:
        raise RuntimeError("boom")

    async def successful_work(value: str) -> str:
        return value

    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await queue.run(failing_work)
        assert await queue.run(lambda: successful_work("ok")) == "ok"
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_waiting_job_times_out_by_wall_clock_without_running_factory() -> None:
    release_first = asyncio.Event()
    second_called = False
    queue = SerialGpuQueue(queue_timeout_seconds=0.2)
    await queue.start()

    async def first() -> None:
        await release_first.wait()

    async def second() -> None:
        nonlocal second_called
        second_called = True

    first_task = asyncio.create_task(queue.run(first))
    await wait_until(lambda: queue.stats().active_count == 1)
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(PipelineError, match="QUEUE_TIMEOUT"):
            await queue.run(second)
        assert asyncio.get_running_loop().time() - started < 1.0
        assert second_called is False
    finally:
        release_first.set()
        await first_task
        await queue.stop()


@pytest.mark.asyncio
async def test_abort_failure_poisons_queue_and_never_runs_next_factory() -> None:
    queue = SerialGpuQueue(queue_timeout_seconds=2)
    await queue.start()
    next_called = False

    async def uncertain_failure() -> None:
        raise PipelineError(
            ErrorCode.ENGINE_UNAVAILABLE,
            "runtime",
            "abort could not be confirmed",
            retryable=False,
            poison_queue=True,
        )

    async def next_work() -> None:
        nonlocal next_called
        next_called = True

    try:
        with pytest.raises(PipelineError, match="ENGINE_UNAVAILABLE"):
            await queue.run(uncertain_failure)
        assert queue.stats().state == "poisoned"
        with pytest.raises(PipelineError, match="ENGINE_UNAVAILABLE"):
            await queue.run(next_work)
        assert next_called is False
    finally:
        await queue.stop()
```

本文件必须定义可运行的 `wait_until(predicate, timeout=1.0)` helper；不得依赖未声明 fixture。

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/unit/test_jobs.py tests/unit/test_gpu_queue.py -q
```

Expected: FAIL，缺少队列与注册表。

- [ ] **Step 3: 实现只有一个 consumer 的队列**

队列不得为每个请求直接 `create_task(work())`。核心结构必须是：

```python
class SerialGpuQueue:
    def __init__(self, queue_timeout_seconds: float) -> None:
        self._items: asyncio.Queue[QueueItem[Any] | None] = asyncio.Queue()
        self._consumer: asyncio.Task[None] | None = None
        self._active_count = 0
        self._max_active_observed = 0
        self._queue_timeout_seconds = queue_timeout_seconds
        self._state: Literal["accepting", "poisoned", "stopping", "stopped"] = "stopped"
        self._poison_reason: str | None = None

    async def start(self) -> None:
        if self._consumer is None:
            self._state = "accepting"
            self._consumer = asyncio.create_task(self._consume(), name="single-gpu-consumer")

    async def run(self, factory: Callable[[], Awaitable[T]]) -> T:
        if self._consumer is None or self._state == "stopped":
            raise RuntimeError("GPU queue is not started")
        if self._state != "accepting":
            raise PipelineError(
                ErrorCode.ENGINE_UNAVAILABLE,
                "queue",
                self._poison_reason or "GPU queue is not accepting work",
                retryable=False,
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        started: asyncio.Future[None] = loop.create_future()
        item = QueueItem(
            factory=factory,
            future=future,
            started=started,
            enqueued_at=loop.time(),
        )
        await self._items.put(item)
        try:
            await asyncio.wait_for(
                asyncio.shield(started),
                timeout=self._queue_timeout_seconds,
            )
        except TimeoutError as exc:
            if not started.done():
                item.cancelled = True
                if not future.done():
                    future.cancel()
                raise PipelineError(
                    ErrorCode.QUEUE_TIMEOUT,
                    "queue",
                    "job expired before GPU execution",
                    retryable=True,
                ) from exc
        try:
            return await future
        except asyncio.CancelledError:
            item.cancelled = True
            raise

    async def _consume(self) -> None:
        while True:
            item = await self._items.get()
            if item is None:
                self._items.task_done()
                return
            entered = False
            try:
                if item.cancelled or item.future.cancelled():
                    continue
                item.started.set_result(None)
                self._active_count += 1
                entered = True
                self._max_active_observed = max(self._max_active_observed, self._active_count)
                result = await item.factory()
                if not item.future.cancelled():
                    item.future.set_result(result)
            except BaseException as exc:
                if isinstance(exc, PipelineError) and exc.poison_queue:
                    self.poison(exc.message)
                if not item.future.cancelled():
                    item.future.set_exception(exc)
            finally:
                if entered:
                    self._active_count -= 1
                self._items.task_done()
```

`QueueItem` 必须显式包含 `started` 和 `cancelled: bool = False`。`run()` 的 timeout 只约束排队阶段：一旦 consumer 设置 `started`，由各 engine 的 `request_timeout_seconds` 约束推理阶段。等待方的 wall-clock timer 必须独立于前一任务，所以即使前项卡住，排队项也能准时得到 `QUEUE_TIMEOUT` 且 factory 永不执行。

`poison(reason)` 必须同步切换 fail-closed 状态，把尚未 started 的队列项全部以 `ENGINE_UNAVAILABLE` 完成，并令后续 `run()` 在入队前失败；当前 factory 仍由 service 的 abort 顺序收尾。`resume_after_verified_recovery(health: RuntimeHealth)` 只有在 `health.status == "ready"`、两个 `active_inference == 0` 且没有 `unknown/unhealthy` worker 时才恢复；否则拒绝。只能由应用生命周期/真实 runtime recovery 路径调用，HTTP 业务路由不暴露手动解除按钮。测试必须证明未验证的 health 不能解除 poison、经验证的零活动 health 才可解除。

`stop(deadline: float, grace_seconds=0.5, abort_active=...)` 是有界关闭而非无限 `join`：立即切换 `stopping`、拒绝新任务、失败所有未执行项；给当前 factory 最多 `min(0.5, deadline-now)` 自行结束；仍 active 时把同一个 absolute monotonic deadline 传给 `abort_active`。确认 active inference 归零后取消/等待 consumer，最终 `active_count == 0` 并进入 `stopped`。任何步骤只能消费 `deadline-now`，不得重新启动一段独立 timeout。abort 无法确认时保持 poisoned/degraded，但关闭流程继续交给 `stop.ps1` 的 PID-tree fallback，不能等待 300 秒 request timeout。不要实现多 consumer 参数。

- [ ] **Step 4: 实现任务记录**

`JobRecord` 必须记录：

```text
job_id
request_id
kind: reference | gsv | segment
status: queued | running | succeeded | failed
stage
created_at
started_at
finished_at
request_snapshot
result
error
```

注册表 `create()` 必须生成新的 server `job_id` 并返回与该 job 绑定的 `ExecutionContext`；相同 `request_id` 可重复创建且 `job_id` 必须不同。更新必须在一个 `asyncio.Lock` 内执行；返回模型副本，调用者不能原地修改注册表内容。

- [ ] **Step 5: 验证、提交**

```powershell
uv run pytest tests/unit/test_jobs.py tests/unit/test_gpu_queue.py -q
uv run ruff check .
uv run mypy src/voice_pipeline
git add src/voice_pipeline/core tests/unit
git commit -m "feat: serialize gpu work through one consumer"
```

Expected: 全部退出码 0，测试观测 `max_active_observed == 1`。

---

### Task 5: 构建 FastAPI 控制面和假后端全链路

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\api\dependencies.py`
- Create: `D:\TTSsystem\src\voice_pipeline\api\routes.py`
- Create: `D:\TTSsystem\src\voice_pipeline\api\app.py`
- Create: `D:\TTSsystem\tests\integration_cpu\test_api_jobs.py`
- Create: `D:\TTSsystem\tests\integration_cpu\test_api_failures.py`

**Interfaces:**
- Consumes: Task 3 service and Task 4 queue/registry.
- Produces: all endpoints in section 3.2.
- Produces: `create_app(settings, *, index_client=None, gsv_client=None, engine_runtime=None) -> FastAPI`.

- [ ] **Step 1: 写 202、轮询、音频下载和健康统计失败测试**

```python
import asyncio

import httpx
import pytest

from voice_pipeline.api.app import create_app


@pytest.mark.asyncio
async def test_segment_job_completes_through_single_queue(fake_settings, request_json) -> None:
    app = create_app(fake_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            submitted = await client.post("/api/v1/jobs/segment", json=request_json)
            assert submitted.status_code == 202
            job_id = submitted.json()["job_id"]

            for _ in range(100):
                status = (await client.get(f"/api/v1/jobs/{job_id}")).json()
                if status["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)

            assert status["status"] == "succeeded"
            assert (await client.get(f"/api/v1/jobs/{job_id}/audio/reference")).status_code == 200
            assert (await client.get(f"/api/v1/jobs/{job_id}/audio/target")).status_code == 200
            assert (
                await client.get(f"/api/v1/jobs/{job_id}/manifest/reference")
            ).status_code == 200
            assert (await client.get(f"/api/v1/jobs/{job_id}/manifest/run")).status_code == 200
            health = (await client.get("/api/v1/health")).json()
            assert health["gpu_queue"]["max_active_observed"] == 1
```

`tests\integration_cpu\conftest.py` 必须定义有效的 `fake_settings` 与 `request_json`，包含实际创建的 5 秒 base voice WAV；不能让示例依赖隐式 fixture。另写两个测试：

1. 两次提交同一 `request_id` 均返回 202、`job_id` 不同，且产物目录互不相同；
2. 前一任务阻塞时，后一任务超过 `queue_timeout_seconds` 后 job 进入 `failed/QUEUE_TIMEOUT`，不能永久停在 `queued`。

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/integration_cpu/test_api_jobs.py tests/integration_cpu/test_api_failures.py -q
```

Expected: FAIL，缺少 FastAPI app。

- [ ] **Step 3: 实现应用生命周期和依赖注入**

`create_app()` 必须：

1. 严格按 `fake | external_test | real` 三种 mode 建立 Task 1 冻结的依赖组合；
2. 显式允许测试注入 clients 和 `EngineRuntime`；
3. 在 lifespan 中先启动 runtime/supervisor，再启动唯一 queue consumer；
4. 关闭时先停止接收请求、失败未执行任务，并创建唯一 absolute monotonic deadline `loop.time() + 5.5`；queue grace、abort、runtime stop 和 Uvicorn callback 全部共享该 deadline，不得各自重置计时；
5. 不在模块导入时创建模型、HTTP client、事件循环任务或全局可变 registry。

`NoopEngineRuntime`、`ExternalEngineRuntime`、`ProcessSupervisor` 都完整实现 `start/stop/ensure_engine/abort_engine/engine_identity/health/begin_inference`。`ExternalEngineRuntime.start()/stop(deadline=...)` 不启动/停止外部进程；`ensure_engine()` 查询对应 health 并逐项核对由配置 challenge 派生的完整 `EngineFingerprint`，`abort_engine()` 调用对应 base URL 的 `POST /__control/abort` 并等待“active request 已归零”的确认。这一约定只在显式 `external_test` mode 生效，真实 mode 一律走 `ProcessSupervisor`。

路由提交任务时立即在 registry 创建 `queued` 记录和 `ExecutionContext`，再将一个闭包交给 `SerialGpuQueue.run()`；闭包中更新 `running` 并将 context 传给 service。包裹 `queue.run()` 的后台协程必须在最外层捕获包括 `QUEUE_TIMEOUT` 在内的任何异常并调用 `mark_failed()`；否则在 factory 执行前超时的 job 会永久停留在 `queued`。成功才调用 `mark_succeeded()`。后台调度任务本身不执行 GPU，只等待 queue future。

manifest 路由从 registry 中经过校验的 result 路径读取文件，不接受任意路径 query；`reference` job 只有 reference manifest，`gsv/segment` 按实际结果暴露对应 manifest。shutdown coordinator 必须幂等：先拒绝新任务并把 queued job 标记失败，创建 `deadline = loop.time() + 5.5`，调用 `queue.stop(deadline=deadline, grace_seconds=0.5, abort_active=...)`。若 grace 后 queue 仍 active，`abort_active` 优先按 `InferenceTracker` 找 active/unknown engine，并为 fail-safe 并发 abort 所有 `ready|starting|unknown` worker（空闲 worker 在关闭阶段也可停止），每个调用接收同一 `deadline`；随后以 `await wait_for(runtime.stop(deadline=deadline), timeout=max(0, deadline-now))` 收尾，最后调用 Uvicorn callback。resident 两 worker 必须并发停止并共享余量。路由只触发并等待这个 coordinator，不得直接 `os._exit()`；lifespan `finally` 重入时不得重复杀 PID。

加入活动请求故障测试：adapter 模拟 300 秒挂起，调用 `/api/v1/control/shutdown` 后必须拒绝新任务、未执行 job 失败、active engine 收到 abort，shutdown coordinator 在 5.5 秒 deadline 内结束；另用两个都会慢停的真实 parent/child fixture 证明 resident stop 并发且不重置 deadline。`stop.ps1` 的独立 process test 再证明全部 PID/子孙最迟 10 秒消失。

- [ ] **Step 4: 固定响应与错误映射**

所有错误响应必须是：

```json
{
  "error": {
    "code": "INDEX_ENGINE_ERROR",
    "stage": "index",
    "message": "concise message",
    "retryable": true,
    "details": {}
  }
}
```

映射固定为：

```text
Pydantic request validation -> HTTP 422 / INVALID_INPUT
unknown job -> HTTP 404
queued/running audio request -> HTTP 409
failed job audio request -> HTTP 409 with stored error
succeeded audio -> HTTP 200 audio/wav
```

健康响应至少包含：

```json
{
  "status": "ready",
  "mode": "fake",
  "engine_lifecycle": "resident",
  "control": {
    "pid": 1234,
    "instance_id": "10676aa6-86e1-424d-a8dd-77f6ce09fc57",
    "python_executable": "D:\\TTSsystem\\.venv\\Scripts\\python.exe",
    "audit_log": "D:\\TTSsystem\\runtime\\logs\\10676aa6-86e1-424d-a8dd-77f6ce09fc57\\engine-audit.jsonl"
  },
  "workers": {
    "indextts": {
      "state": "ready",
      "pid": 1234,
      "create_time": 1786000000.0,
      "python_executable": "D:\\TTSsystem\\.venv\\Scripts\\python.exe",
      "python_version": "3.11.13",
      "source_revision": "in-process-fake",
      "fingerprint": {"schema_version": 1, "engine": "indextts", "source_revision": "in-process-fake", "model_revision": "1", "engine_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "checkpoint_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "environment_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "runtime_config_sha256": "0000000000000000000000000000000000000000000000000000000000000001"},
      "preflight_ok": true,
      "active_inference": 0
    },
    "gpt_sovits": {
      "state": "ready",
      "pid": 1234,
      "create_time": 1786000000.0,
      "python_executable": "D:\\TTSsystem\\.venv\\Scripts\\python.exe",
      "python_version": "3.11.13",
      "source_revision": "in-process-fake",
      "fingerprint": {"schema_version": 1, "engine": "gpt_sovits", "source_revision": "in-process-fake", "model_revision": "1", "engine_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "checkpoint_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "environment_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "runtime_config_sha256": "0000000000000000000000000000000000000000000000000000000000000002"},
      "preflight_ok": true,
      "active_inference": 0
    }
  },
  "gpu_queue": {
    "state": "accepting",
    "poison_reason": null,
    "active_count": 0,
    "queued_count": 0,
    "max_active_observed": 1,
    "max_concurrency": 1
  }
}
```

所有 runtime、health、doctor、PID registry、audit 和 verifier 的 engine key 固定为 `indextts | gpt_sovits`；`indextts2` 只允许作为 Python 包/产品名称，绝不能作为 runtime JSON key。worker 健康对象统一使用 `WorkerHealth`，状态枚举固定为 `ready | stopped_expected | starting | unhealthy | unknown`。`resident` 只有两个 worker 都 `ready` 才是 overall ready；`exclusive_process` 允许零或一个 worker 处于 `ready`、另一项为 `stopped_expected`，并要求配置/解释器/源码/权重预检均成功。queue poisoned 或任一 unknown 时 overall 必须为 degraded。该规则供 Task 9 doctor 和 Gate G 复用。

- [ ] **Step 5: 验证、提交**

```powershell
uv run pytest tests/integration_cpu/test_api_jobs.py tests/integration_cpu/test_api_failures.py -q
uv run ruff check .
uv run mypy src/voice_pipeline
git add src/voice_pipeline/api tests/integration_cpu
git commit -m "feat: expose batch one in-memory job api"
```

Expected: 全部退出码 0。

---

### Task 6: 实现只走控制面的稳定 CLI

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\cli.py`
- Create: `D:\TTSsystem\src\voice_pipeline\main.py`
- Create: `D:\TTSsystem\src\voice_pipeline\__main__.py`
- Create: `D:\TTSsystem\tests\unit\test_cli.py`
- Create: `D:\TTSsystem\tests\contract\test_cli_json_contract.py`

**Interfaces:**
- Produces: section 3.1 的五个命令。
- Guarantees: generation commands are HTTP-only, JSON stdout is machine-readable, output files are downloaded atomically.

- [ ] **Step 1: 写控制面不可达和 JSON stdout 失败测试**

```python
import json

from typer.testing import CliRunner

from voice_pipeline.cli import app


runner = CliRunner()


def test_synthesize_never_falls_back_when_server_is_down(tmp_path) -> None:
    base_voice = tmp_path / "base.wav"
    write_tone(base_voice, seconds=5.0)
    request = tmp_path / "request.json"
    request.write_text(
        SegmentSynthesisRequest(
            request_id="d955a4a2-bf44-4a49-a82c-2962eb602d75",
            base_voice_path=base_voice.resolve(),
            ref_text_cn="我依然会向前走。",
            emotion_vector=[0.0, 0.0, 0.2, 0.0, 0.0, 0.2, 0.0, 0.2],
            target_text="I will keep moving forward.",
            target_language="en",
            seed=1234,
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "synthesize-segment",
            "--server",
            "http://127.0.0.1:1",
            "--request",
            str(request),
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "CONTROL_PLANE_UNAVAILABLE"
    assert not (tmp_path / "out").exists()
```

测试文件必须显式 import `SegmentSynthesisRequest` 并定义 `write_tone()`；这里刻意使用有效请求，确保失败发生在 HTTP 连接而非本地 Schema 校验。另加非法 `{}` 请求测试，断言退出码 2/`INVALID_INPUT` 且不发 HTTP。

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/unit/test_cli.py tests/contract/test_cli_json_contract.py -q
```

Expected: FAIL，缺少 CLI。

- [ ] **Step 3: 实现 CLI 公共行为**

实现规则：

- `serve` 是唯一在本进程创建 FastAPI app 的命令。它必须显式构造 `uvicorn.Config(app, host=..., port=..., workers=1, reload=False)` 与 `uvicorn.Server(config)`，把幂等 callback（设置该实例的 `server.should_exit = True`）注入 shutdown coordinator 后再 `server.run()`；禁止使用不给路由持有 Server 句柄的 `uvicorn.run()` convenience API。
- `serve` 不暴露 `--workers` 或 `--reload`；真实与 fake 模式都固定单进程、`reload=False`。
- 四个客户端命令只使用 `httpx.Client`；禁止 import `modules.indextts.client`、`modules.gpt_sovits.client` 或 worker。
- 请求 JSON 必须先在 CLI 本地做 schema 校验，再 POST。
- 轮询间隔固定 250ms，CLI 总等待超时由 `--timeout-seconds` 指定，默认 900 秒。
- 每个目标先调用 `reserve_output_path()`；下载到同目录 UUID partial，验证 WAV 后用 reservation 发布；失败只回滚本次拥有的 reservation。
- `generate-reference --output X.wav` 同时创建 `X.reference-manifest.json`：从 `/manifest/reference` 下载服务端 manifest，验证下载 WAV 的 SHA-256，再只把 `audio.path` 改成下载后 `X.wav` 的绝对路径并原子写出。后续 `generate-gsv` 请求必须引用该 portable manifest。
- `generate-gsv --output X.wav` 只下载目标音频，不创建或改写任何参考音频与 reference manifest。
- `synthesize-segment --output-dir DIR` 从两个 manifest endpoint 下载并发布 `reference.wav`、`reference-manifest.json`、`target.wav` 和 `run-manifest.json`；四个固定最终路径中任一已存在时必须在下载前返回 `OUTPUT_CONFLICT`。
- `--json` 把恰好一个成功或错误对象写 stdout；日志使用 `typer.echo(..., err=True)`。
- 如果输出目标已经存在，返回退出码 2 和 `OUTPUT_CONFLICT`，不得覆盖。

contract tests 必须预写随机 sentinel 并比较前后 SHA-256；还必须证明 `generate-gsv` 前后输入 reference WAV 与 reference manifest 的 SHA-256 完全不变、Index 调用数为 0、GSV 收到的 reference SHA-256 与 manifest 一致。

`__main__.py` 只包含：

```python
from voice_pipeline.cli import app

if __name__ == "__main__":
    app()
```

- [ ] **Step 4: 验证 wheel 外部调用契约**

```powershell
uv run pytest tests/unit/test_cli.py tests/contract/test_cli_json_contract.py -q
uv build --wheel --out-dir runtime/wheel-test
$Wheel = (Get-ChildItem 'runtime\wheel-test\*.whl' | Select-Object -First 1).FullName
uv venv runtime/wheel-venv --python 3.11
uv pip install --python runtime/wheel-venv/Scripts/python.exe $Wheel
Push-Location $env:TEMP
& 'D:\TTSsystem\runtime\wheel-venv\Scripts\python.exe' -m voice_pipeline --help
Pop-Location
```

Expected: tests PASS；从仓库外运行 `--help` 退出码 0。

- [ ] **Step 5: 提交**

```powershell
git add src/voice_pipeline tests/unit/test_cli.py tests/contract/test_cli_json_contract.py
git commit -m "feat: add control-plane-only cli"
```

---

### Task 7: 实现 IndexTTS2 薄 HTTP worker 与控制面 adapter

**Files:**
- Create: `D:\TTSsystem\workers\indextts2\requirements.txt`
- Create: `D:\TTSsystem\workers\indextts2\schemas.py`
- Create: `D:\TTSsystem\workers\indextts2\engine.py`
- Create: `D:\TTSsystem\workers\indextts2\app.py`
- Create: `D:\TTSsystem\workers\indextts2\__main__.py`
- Create: `D:\TTSsystem\src\voice_pipeline\modules\indextts\client.py`
- Create: `D:\TTSsystem\tests\contract\test_indextts_client.py`
- Create: `D:\TTSsystem\tests\contract\test_indextts_worker.py`

**Interfaces:**
- Produces: Index worker `/health/live`, `/health/ready`, `/v1/synthesize`, `/v1/control/stop`.
- Produces: `IndexTTSHttpClient` implementing `IndexTTSClient`.
- Guarantees: effective vector echo equals request exactly; worker output path is restricted to configured runtime jobs root.

- [ ] **Step 1: 写 adapter 请求契约失败测试**

```python
import httpx
import pytest

from voice_pipeline.modules.indextts.client import IndexTTSHttpClient


@pytest.mark.asyncio
async def test_index_client_sends_exact_vector_and_absolute_paths(index_request, tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.json())
        worker_output = Path(str(seen["output_path"]))
        write_valid_reference(worker_output, seconds=4.0)
        return httpx.Response(
            200,
            json={
                "request_id": str(index_request.request_id),
                "output_path": str(worker_output.resolve()),
                "effective_emotion_vector": list(index_request.emotion_vector),
                "engine_fingerprint": EXPECTED_INDEX_FINGERPRINT,
            },
        )

    client = IndexTTSHttpClient(
        base_url="http://index.test",
        timeout_seconds=5,
        jobs_root=tmp_path,
        expected_fingerprint=EXPECTED_INDEX_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    await client.synthesize(index_request, tmp_path / "reference.wav")

    assert seen["emotion_vector"] == list(index_request.emotion_vector)
    assert seen["use_random"] is False
    assert seen["speaker_audio_path"] == str(index_request.speaker_audio_path)
```

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/contract/test_indextts_client.py tests/contract/test_indextts_worker.py -q
```

Expected: FAIL，缺少 worker 和 adapter。

- [ ] **Step 3: 实现 worker engine，禁止隐藏向量修改**

`workers\indextts2\requirements.txt` 固定为：

```text
fastapi==0.115.12
pydantic==2.10.6
uvicorn[standard]==0.34.3
```

`workers\indextts2\engine.py` 必须延迟 import 上游包，使无 GPU contract test 可以注入 fake engine。真实实现的核心调用固定为：

```python
import os
import random
from pathlib import Path
from uuid import uuid4

import numpy as np
import torch
from indextts.infer_v2 import IndexTTS2

from .schemas import WorkerSynthesisRequest
from voice_pipeline.modules.audio.atomic_output import reserve_output_path


class RealIndexEngine:
    def __init__(
        self,
        model_dir: Path,
        aux_paths: dict[str, str],
        device: str = "cuda:0",
    ) -> None:
        required_aux = {"w2v_bert", "semantic_codec", "campplus", "bigvgan"}
        if set(aux_paths) != required_aux:
            raise ValueError("all four pinned auxiliary model paths are required")
        self._tts = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"),
            model_dir=str(model_dir),
            use_fp16=True,
            device=device,
            use_cuda_kernel=False,
            use_deepspeed=False,
            use_accel=False,
            use_torch_compile=False,
            aux_paths=aux_paths,
        )

    def synthesize(self, request: WorkerSynthesisRequest) -> None:
        random.seed(request.seed)
        np.random.seed(request.seed % (2**32))
        torch.manual_seed(request.seed)
        torch.cuda.manual_seed_all(request.seed)
        reservation = reserve_output_path(request.output_path)
        partial = request.output_path.with_name(
            f".{request.output_path.stem}.{uuid4()}.partial.wav"
        )
        try:
            self._tts.infer(
                spk_audio_prompt=str(request.speaker_audio_path),
                text=request.text,
                output_path=str(partial),
                emo_alpha=1.0,
                emo_vector=list(request.emotion_vector),
                use_emo_text=False,
                emo_text=None,
                use_random=False,
                verbose=False,
            )
            if not partial.is_file() or partial.stat().st_size == 0:
                raise RuntimeError("IndexTTS2 did not create a non-empty WAV")
            reservation.publish(partial)
        except BaseException:
            reservation.rollback()
            raise
        finally:
            partial.unlink(missing_ok=True)
```

禁止调用 `normalize_emo_vec()`。worker 返回的 `effective_emotion_vector` 必须直接 echo 已验证请求值。`aux_paths` 必须由 CLI 参数/lock 解析为 setup 创建的四个本地路径；不得省略以触发上游 `ensure_models_available()` 的可变 `main` 下载。

- [ ] **Step 4: 实现 worker HTTP 与路径限制**

worker 启动参数必须包含：

```text
--host 127.0.0.1
--port 9871
--repo-dir D:\TTSsystem\external\index-tts
--model-dir D:\TTSsystem\external\index-tts\checkpoints
--aux-root D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned
--jobs-root D:\TTSsystem\runtime\jobs
--engine-lock D:\TTSsystem\config\engines.lock.yaml
--checkpoint-lock D:\TTSsystem\config\checkpoints.lock.yaml
--environment-lock D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt
--environment-freeze D:\TTSsystem\config\env-locks\index-pip-freeze.txt
--expected-fingerprint-json $CanonicalIndexFingerprintJson
```

进程固定从项目根启动：

```powershell
Set-Location 'D:\TTSsystem'
& $IndexPython -m workers.indextts2 @WorkerArgs
```

`workers` 两级目录都必须有 `__init__.py`，内部一律使用相对 import。`__main__.py` 在 import `.app/.engine` 前先仅用标准库解析参数，把经过校验的 `--repo-dir` 插入 `sys.path`，随后才导入 worker app/上游 `indextts`；supervisor 不得把 cwd 切到 engine repo。Index 环境 setup 需以 `--no-deps` 安装本项目 wheel，使 worker 复用同一个 `atomic_output` 实现。

`POST /v1/synthesize` 接收的是控制 adapter 分配的独立 working WAV 路径；解析后必须满足：

```python
resolved_output.is_relative_to(resolved_jobs_root)
```

并且扩展名为 `.wav`。worker 拒绝 `..\`、符号链接逃逸、已存在文件和非绝对路径。worker 必须在模型 import 前解析 `--expected-fingerprint-json` 为固定 `EngineFingerprint`，再从 engine/checkpoint/environment requirements+freeze、源码/model revision 与实际 `config.yaml` 重算全部 8 个字段并逐项比较；任一不符立即失败，不能启动为 ready。`/health/ready` 只有上述 fingerprint handshake、模型实例初始化和 CUDA 检查全部成功时才返回统一 `WorkerHealth` 字段，`fingerprint` 必须与 supervisor 预计算值逐项相同。`POST /v1/control/stop` 只接受 loopback，在响应发出后安排正常 SIGTERM；supervisor 仍需在 deadline 内执行进程树 terminate/kill fallback。

- [ ] **Step 5: 实现控制面 Index adapter**

adapter 必须：

- 构造时接收 `jobs_root` 与由 lock/doctor 预计算的 `expected_fingerprint`；同步 `fingerprint()` 只返回该不可变副本，不临时发 HTTP；
- 用 `reserve_output_path(output_path)` 保留最终 service 路径，并在其父目录生成 UUID working WAV；
- 用 Pydantic `model_dump(mode="json")` 序列化；
- 把 request/read timeout 映射为 `INDEX_TIMEOUT`；请求已送出后的 reset、截断 body、stream read error 映射为 `INDEX_ENGINE_ERROR`，二者均带 `requires_engine_abort=True`；
- 连接建立前失败与完整非 2xx 映射为 `INDEX_ENGINE_ERROR` 且 `requires_engine_abort=False`；
- 核对响应 `request_id`；
- 核对 `effective_emotion_vector` 与原请求逐项完全相等；
- 核对响应 `engine_fingerprint` 与构造时的预期值相等；
- 忽略 worker 自报音频指标，由控制面 `probe_wav()` 重算；
- 确保响应输出路径就是控制面分配的 working 路径；
- 探测 working WAV 成功后才由 final reservation 发布；任何错误回滚 final reservation 并删除本次 working 文件。

HTTP adapter 不能声称 timeout/reset/截断流已经取消远端推理。Task 3 的 service 必须随后等待 runtime 清理；contract test 用 `RecordingEngineRuntime` 逐一断言 request timeout、read timeout、post-dispatch reset、截断流和 cancellation 都在 queue future 完成前调用 `abort_engine("indextts")`，而 connect-before-dispatch failure/完整 HTTP 500 不调用。完成状态不确定错误的 `details.owned_temporary_paths` 必须包含 control 分配的 working WAV 和本次 final reservation；worker 被强杀后，service 再做一次受 job-root 约束的清理，防止 worker 在 adapter 首次 cleanup 后又落盘。

- [ ] **Step 6: 验证、提交**

```powershell
uv run pytest tests/contract/test_indextts_client.py tests/contract/test_indextts_worker.py -q
uv run ruff check .
uv run mypy src/voice_pipeline workers
git add workers/indextts2 src/voice_pipeline/modules/indextts tests/contract
git commit -m "feat: add isolated indextts2 worker adapter"
```

Expected: 全部退出码 0。

---

### Task 8: 实现 GPT-SoVITS adapter 和可切换进程 supervisor

**Files:**
- Create: `D:\TTSsystem\src\voice_pipeline\modules\gpt_sovits\client.py`
- Create: `D:\TTSsystem\src\voice_pipeline\runtime\process.py`
- Create: `D:\TTSsystem\src\voice_pipeline\runtime\supervisor.py`
- Create: `D:\TTSsystem\tests\contract\test_gpt_sovits_client.py`
- Create: `D:\TTSsystem\tests\process\test_supervisor.py`

**Interfaces:**
- Produces: `GptSoVitsHttpClient` implementing `GptSoVitsClient`.
- Produces: `ProcessSupervisor.start/ensure_engine/begin_inference/abort_engine/stop(deadline)/health`.
- Supports: `resident` and `exclusive_process` without changing callers.

- [ ] **Step 1: 写官方 `/tts` payload 契约失败测试**

```python
import httpx
import pytest

from voice_pipeline.modules.gpt_sovits.client import GptSoVitsHttpClient


@pytest.mark.asyncio
async def test_gsv_payload_uses_bound_reference_text(gsv_request, tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.json())
        return httpx.Response(200, content=valid_wav_bytes(seconds=1.5))

    client = GptSoVitsHttpClient(
        base_url="http://gsv.test",
        timeout_seconds=5,
        expected_fingerprint=EXPECTED_GSV_FINGERPRINT,
        transport=httpx.MockTransport(handler),
    )
    await client.synthesize(gsv_request, tmp_path / "target.wav")

    assert seen == {
        "text": gsv_request.text,
        "text_lang": gsv_request.text_lang,
        "ref_audio_path": str(gsv_request.reference.audio.path),
        "prompt_text": gsv_request.reference.ref_text_cn,
        "prompt_lang": "zh",
        "top_k": 15,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut0",
        "batch_size": 1,
        "split_bucket": False,
        "speed_factor": gsv_request.speed_factor,
        "fragment_interval": 0.0,
        "seed": gsv_request.seed,
        "parallel_infer": False,
        "repetition_penalty": 1.35,
        "media_type": "wav",
        "streaming_mode": False,
    }
```

- [ ] **Step 2: 写 supervisor 生命周期失败测试**

```python
@pytest.mark.asyncio
async def test_exclusive_mode_never_keeps_both_workers_running(fake_processes) -> None:
    supervisor = ProcessSupervisor(mode="exclusive_process", processes=fake_processes)
    await supervisor.start()
    await supervisor.ensure_engine("indextts")
    assert fake_processes.running_names() == {"indextts"}
    await supervisor.ensure_engine("gpt_sovits")
    assert fake_processes.running_names() == {"gpt_sovits"}
    await supervisor.stop()
    assert fake_processes.running_names() == set()
```

`tests\contract\conftest.py` 必须实现 `gsv_request`、`valid_wav_bytes`、`EXPECTED_GSV_FINGERPRINT`；`tests\process\conftest.py` 必须实现会真实创建 parent/child 的 `fake_processes`。这些 helper 都要在对应 Task 测试命令中被实际执行。

再写 timeout 清理测试：模拟 worker 及其 child 仍运行，调用 `await supervisor.abort_engine("gpt_sovits", reason="timeout")`，断言 parent/child 均退出、PID registry 已原子移除该 worker，且该 await 返回之前不能记录下一次 `ensure_engine()`。

- [ ] **Step 3: 运行并确认失败**

```powershell
uv run pytest tests/contract/test_gpt_sovits_client.py tests/process/test_supervisor.py -q
```

Expected: FAIL，缺少 GSV adapter 和 supervisor。

- [ ] **Step 4: 实现 GSV adapter**

adapter 把 section 3 的 `GsvSynthesisRequest` 映射为上面固定 payload。成功响应必须：

1. HTTP 200；
2. Content-Type 不是 JSON 或 text；
3. 用 `reserve_output_path(output_path)` 独占最终目标；
4. 按 UUID 写入同目录 partial；
5. 从 HTTP 流写完、flush/fsync 后再继续；
6. 用控制面的 `probe_wav()` 独立验证；
7. 由 reservation 发布 partial；
8. 再次 probe 最终路径并返回。

完整 HTTP 400/500 映射为 `GSV_ENGINE_ERROR` 且 `requires_engine_abort=False`；request/read timeout 映射为 `GSV_TIMEOUT`，请求已送出后的 reset、截断 WAV stream 和 read error 映射为 `GSV_ENGINE_ERROR`，这些完成状态不确定错误均令 `requires_engine_abort=True`。错误响应正文最多保留 2KB 到诊断信息，避免日志爆炸。contract tests 必须覆盖与 Index adapter 相同的 post-dispatch uncertainty/cancellation 矩阵。

构造函数必须接收从 lock/doctor 计算出的 `expected_fingerprint`，`fingerprint()` 同步返回其不可变副本。因为官方 `/tts` 不返回指纹，真实 mode 在创建 adapter 前必须由 supervisor 对源码、配置和 checkpoint 文件逐一算 SHA-256 并与 lock 核对；不一致时不能启动 GSV。

- [ ] **Step 5: 实现安全 Windows 进程管理**

`ManagedProcess` 必须使用：

```python
subprocess.Popen(
    args,
    cwd=str(cwd),
    stdin=subprocess.DEVNULL,
    stdout=stdout_log,
    stderr=stderr_log,
    shell=False,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
```

启动命令：

```text
Index:
D:\TTSsystem\external\index-tts\.venv\Scripts\python.exe
  -m workers.indextts2
  --host 127.0.0.1
  --port 9871
  --repo-dir D:\TTSsystem\external\index-tts
  --model-dir D:\TTSsystem\external\index-tts\checkpoints
  --aux-root D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned
  --jobs-root D:\TTSsystem\runtime\jobs
  --engine-lock D:\TTSsystem\config\engines.lock.yaml
  --checkpoint-lock D:\TTSsystem\config\checkpoints.lock.yaml
  --environment-lock D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt
  --environment-freeze D:\TTSsystem\config\env-locks\index-pip-freeze.txt
  --expected-fingerprint-json $CanonicalIndexFingerprintJson
  --device cuda:0
  --fp16

GSV:
D:\TTSsystem\external\GPT-SoVITS\.conda\python.exe
  D:\TTSsystem\external\GPT-SoVITS\api_v2.py
  -a 127.0.0.1
  -p 9880
  -c D:\TTSsystem\external\GPT-SoVITS\GPT_SoVITS\configs\tts_infer.yaml
```

Index 命令的 cwd 固定为 `D:\TTSsystem`；GSV 命令的 cwd 固定为其 repo root。启动后轮询各自健康端点，超时则收集日志尾部、终止进程并返回 `ENGINE_UNAVAILABLE`。正常 engine 切换可使用配置的 transition timeout；但 `stop(deadline=...)`/`abort_engine(deadline=...)` 必须严格消费调用者传入的同一个 absolute monotonic deadline：先尝试 Index `/v1/control/stop` 或 GSV 官方 `GET /control?command=exit`，再按剩余预算 terminate/kill，绝不能每个 worker各等 10 秒。resident 的两个空闲 worker并发停止。使用 `psutil.Process(pid)` 并核对记录的 create time，递归处理 children；只按已记录 PID 操作，禁止按模糊进程名批量结束。

Index readiness 使用 `/health/ready`。官方 GPT-SoVITS 没有专用 health route，因此 readiness 使用 `GET /docs` 返回 200，并额外用 `psutil.net_connections(kind="tcp")` 验证 `127.0.0.1:9880` 的 LISTEN socket owner PID 正是本次记录 PID，且 process create-time 相同；旧端口/别的进程响应必须判失败。不得通过实际 `/tts` 请求伪造健康检查。

- [ ] **Step 6: 实现两种生命周期**

```text
resident:
  start() 顺序启动并等待 Index ready，再启动并等待 GSV ready；之后两者驻留
  ensure_engine() 只检查 ready

exclusive_process:
  start() 不加载模型
  ensure_engine(target) 先停止另一个，再启动 target 并等 ready
```

`ProcessSupervisor.abort_engine(engine, reason, deadline=None)` 无论生命周期为何，都必须把该 worker 标记为 `unknown`，使用调用者 deadline；普通推理错误未传 deadline 时内部只创建一次 `now+5s` deadline。它在该唯一预算内停止/强杀完整进程树并轮询 PID/子孙消失，再返回；返回前不得允许下一个 queue item 执行。下一次 `ensure_engine()` 必须重新启动该 worker。若无法确认退出，抛出带 `poison_queue=True` 的 `ENGINE_UNAVAILABLE`，overall health 进入 `degraded`；queue consumer 据此 poison 并拒绝所有新 GPU 任务。若随后有受管恢复流程，只有 `runtime.health()` 证明两个 `active_inference == 0` 且无 unknown/unhealthy 后，才能调用 `resume_after_verified_recovery()`。

`engine_identity()` 只在该 engine 为 ready 时返回强类型 `EngineIdentity`；stopped/unhealthy/unknown 时抛 `ENGINE_UNAVAILABLE`。`health()` 始终返回统一 `RuntimeHealth/WorkerHealth`；Index `/health/ready` fingerprint 必须与 supervisor 的预计算 fingerprint 完全相同。supervisor 是 `runtime\run\processes.json` 的唯一 owner。每次 control 注册、worker starting/ready/stopped/aborted、生命周期切换都通过“同目录临时 JSON + flush/fsync + `os.replace()`”原子更新：

```json
{
  "schema_version": 1,
  "instance_id": "10676aa6-86e1-424d-a8dd-77f6ce09fc57",
  "audit_log": "D:\\TTSsystem\\runtime\\logs\\10676aa6-86e1-424d-a8dd-77f6ce09fc57\\engine-audit.jsonl",
  "control": {"pid": 123, "create_time": 1.0},
  "engine_lifecycle": "exclusive_process",
  "workers": {
    "indextts": {
      "state": "stopped_expected",
      "pid": null,
      "create_time": null,
      "python_executable": "D:\\TTSsystem\\external\\index-tts\\.venv\\Scripts\\python.exe",
      "python_version": "3.11.13",
      "source_revision": "90ca4d608209584bad3a5bd5becc0b80c146e60f",
      "fingerprint": {"schema_version": 1, "engine": "indextts", "source_revision": "90ca4d608209584bad3a5bd5becc0b80c146e60f", "model_revision": "740dcaff396282ffb241903d150ac011cd4b1ede", "engine_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "checkpoint_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "environment_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000001", "runtime_config_sha256": "0000000000000000000000000000000000000000000000000000000000000001"},
      "preflight_ok": true,
      "active_inference": 0
    },
    "gpt_sovits": {
      "state": "ready",
      "pid": 456,
      "create_time": 2.0,
      "python_executable": "D:\\TTSsystem\\external\\GPT-SoVITS\\.conda\\python.exe",
      "python_version": "3.11.13",
      "source_revision": "d523079fc05d9a8028d6085bffe4a2757c32abb6",
      "fingerprint": {"schema_version": 1, "engine": "gpt_sovits", "source_revision": "d523079fc05d9a8028d6085bffe4a2757c32abb6", "model_revision": "4fae8ec36d3d0373864e580b5d8acfba8da29630", "engine_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "checkpoint_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "environment_lock_sha256": "0000000000000000000000000000000000000000000000000000000000000002", "runtime_config_sha256": "0000000000000000000000000000000000000000000000000000000000000002"},
      "preflight_ok": true,
      "active_inference": 0
    }
  },
  "updated_at_utc": "2026-08-07T00:00:00Z"
}
```

`start.ps1` 不得另写一份静态 PID 清单。队列 consumer 调用应用服务前先执行相应 `ensure_engine()`；切换、启动、停止也位于该 job 的 GPU 队列临界区内。

- [ ] **Step 7: 验证、提交**

```powershell
uv run pytest tests/contract/test_gpt_sovits_client.py tests/process/test_supervisor.py -q
uv run ruff check .
uv run mypy src/voice_pipeline workers
git add src/voice_pipeline/modules/gpt_sovits src/voice_pipeline/runtime tests
git commit -m "feat: add gpt-sovits adapter and worker supervisor"
```

Expected: 全部退出码 0。

---

### Task 9: 编写 Windows 环境脚本、doctor 和模型指纹

**Files:**
- Create: `D:\TTSsystem\config\checkpoints.lock.yaml`
- Create: `D:\TTSsystem\config\env-locks\control-runtime-requirements.lock.txt`
- Create: `D:\TTSsystem\config\env-locks\control-runtime-freeze.txt`
- Create: `D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt`
- Create: `D:\TTSsystem\config\env-locks\index-pip-freeze.txt`
- Create: `D:\TTSsystem\config\env-locks\gsv-conda-explicit.txt`
- Create: `D:\TTSsystem\config\env-locks\gsv-pip-requirements.lock.txt`
- Create: `D:\TTSsystem\config\env-locks\gsv-pip-freeze.txt`
- Create: `D:\TTSsystem\scripts\setup-control.ps1`
- Create: `D:\TTSsystem\scripts\export-environment-inventory.py`
- Create: `D:\TTSsystem\scripts\setup-indextts.ps1`
- Create: `D:\TTSsystem\scripts\setup-gpt-sovits.ps1`
- Create: `D:\TTSsystem\scripts\lock-engine-assets.ps1`
- Create: `D:\TTSsystem\scripts\start.ps1`
- Create: `D:\TTSsystem\scripts\stop.ps1`
- Create: `D:\TTSsystem\src\voice_pipeline\runtime\fingerprints.py`
- Create: `D:\TTSsystem\tests\unit\test_fingerprints.py`
- Create: `D:\TTSsystem\tests\contract\test_doctor.py`
- Create: `D:\TTSsystem\tests\process\test_start_stop_scripts.py`

**Interfaces:**
- Produces: idempotent environment setup, immutable source/model pins and environment snapshots.
- Produces: `voice-pipeline doctor --server ... --json`.
- Produces: SHA-256 fingerprints for code revision, config and model weights.
- Produces: `start.ps1 -Config PATH [-PythonExecutable PATH] -Json` and `stop.ps1 -RunFile PATH -ReceiptPath PATH -Json`.

- [ ] **Step 1: 写 revision、三环境隔离、生命周期感知 doctor 和 PID 失败测试**

```python
def test_doctor_rejects_any_shared_interpreter(doctor_payload) -> None:
    doctor_payload["control"]["python_executable"] = "D:/same/python.exe"
    doctor_payload["workers"]["indextts"]["python_executable"] = "D:/same/python.exe"
    result = validate_doctor_payload(doctor_payload)
    assert result.status == "failed"
    assert "ENVIRONMENTS_NOT_ISOLATED" in result.codes


def test_sha256_file_changes_when_weight_changes(tmp_path) -> None:
    weight = tmp_path / "model.pth"
    weight.write_bytes(b"a")
    first = sha256_file(weight)
    weight.write_bytes(b"b")
    assert sha256_file(weight) != first


def test_exclusive_doctor_accepts_expected_stopped_worker(doctor_payload) -> None:
    doctor_payload["engine_lifecycle"] = "exclusive_process"
    doctor_payload["workers"]["indextts"]["state"] = "ready"
    doctor_payload["workers"]["gpt_sovits"]["state"] = "stopped_expected"
    assert validate_doctor_payload(doctor_payload).status == "ready"
```

`tests\contract\conftest.py` 必须定义完整、严格符合 `RuntimeHealth/WorkerHealth` 的 `doctor_payload` 与 `validate_doctor_payload()`；另测 `resident` 下任一 worker 非 `ready` 必须失败、checkpoint digest 不匹配必须失败、任一 `uv.lock`/env-lock digest 或 live inventory 不匹配必须失败、PID registry 切换后旧 PID 不再出现。所有 JSON 的 runtime key 只使用 `indextts`，不使用 `indextts2`。

- [ ] **Step 2: 运行并确认失败**

```powershell
uv run pytest tests/unit/test_fingerprints.py tests/contract/test_doctor.py tests/process/test_start_stop_scripts.py -q
```

Expected: FAIL，缺少 fingerprint/doctor。

- [ ] **Step 3: 实现 setup-control.ps1**

root `.venv` 只用于开发测试；生产 control 固定为独立 `D:\TTSsystem\.venv-control`，不得把 `--extra dev` inventory 当作运行时 fingerprint。`control-runtime-requirements.lock.txt` 必须由根 `uv.lock` 用固定参数 `uv export --frozen --no-dev --no-emit-project --format requirements.txt` 生成并保留 hashes；fingerprint 同时记录 `profile=runtime` 和这些 export args。脚本必须执行并检查每一步退出码：

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location 'D:\TTSsystem'
uv python install 3.11
uv sync --frozen --extra dev --python 3.11
uv run python -c "import sys; assert sys.version_info[:2] == (3, 11); print(sys.executable)"
uv venv D:\TTSsystem\.venv-control --python 3.11
uv pip sync --python D:\TTSsystem\.venv-control\Scripts\python.exe --require-hashes `
  D:\TTSsystem\config\env-locks\control-runtime-requirements.lock.txt
uv build --wheel --out-dir D:\TTSsystem\runtime\control-wheel
$ControlWheel = (Get-ChildItem D:\TTSsystem\runtime\control-wheel\*.whl |
  Select-Object -First 1).FullName
uv pip install --python D:\TTSsystem\.venv-control\Scripts\python.exe `
  --no-deps --force-reinstall $ControlWheel
```

`scripts\export-environment-inventory.py` 只能用标准库 `importlib.metadata`：把 distribution name 按 PEP 503 规范化，排除 `pip/setuptools/wheel` bootstrap tools，忽略 `direct_url.json`/安装路径，按 name 排序输出 UTF-8 LF 的 `name==version`；因此同一 wheel 从不同目录安装仍得到相同 inventory。control/Index/GSV 都复用它，不能各写一套 freeze 逻辑。只有 `-WriteInitialEnvLocks` 且文件尚不存在时可原子写 runtime requirements lock 和规范化 `control-runtime-freeze.txt`；默认运行重导出 candidate、重采样 `.venv-control` inventory 并 byte-compare。`start.ps1` 默认使用 `.venv-control`；Gate B 的 temp clean control 也必须消费同一 tracked runtime lock并比较同一 freeze。dev `.venv` 永不进入 runtime/doctor fingerprint。

- [ ] **Step 4: 实现幂等且模型 revision 固定的 setup-indextts.ps1**

脚本必须是“create if absent, otherwise verify”。已有目录时核对 remote、干净工作树和精确 HEAD；已有 `.venv` 时核对 Python 3.11，不能再次 clone 或无条件重建。核心命令：

```powershell
git lfs install
if (-not (Test-Path D:\TTSsystem\external\index-tts\.git)) {
  git clone https://github.com/index-tts/index-tts.git D:\TTSsystem\external\index-tts
}
git -C D:\TTSsystem\external\index-tts checkout 90ca4d608209584bad3a5bd5becc0b80c146e60f
git -C D:\TTSsystem\external\index-tts lfs pull
uv venv D:\TTSsystem\external\index-tts\.venv --python 3.11 --seed
uv pip sync `
  --python D:\TTSsystem\external\index-tts\.venv\Scripts\python.exe `
  --require-hashes D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt
uv build --wheel --out-dir D:\TTSsystem\runtime\bootstrap-wheel
$ProjectWheel = (Get-ChildItem D:\TTSsystem\runtime\bootstrap-wheel\*.whl |
  Select-Object -First 1).FullName
uv pip install --python D:\TTSsystem\external\index-tts\.venv\Scripts\python.exe `
  --no-deps --force-reinstall $ProjectWheel
uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
  IndexTeam/IndexTTS-2 `
  --revision 740dcaff396282ffb241903d150ac011cd4b1ede `
  --local-dir D:\TTSsystem\external\index-tts\checkpoints
uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
  facebook/w2v-bert-2.0 `
  --revision da985ba0987f70aaeb84a80f2851cfac8c697a7b `
  --local-dir D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned\w2v_bert
uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
  amphion/MaskGCT semantic_codec/model.safetensors `
  --revision b9ccc6487b9f486b5b4c22c93010e0b54ddce2e2 `
  --local-dir D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned\maskgct
uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
  funasr/campplus campplus_cn_common.bin `
  --revision e4b6ede7ce16997aff4ae69fbca1f0175e2afede `
  --local-dir D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned\campplus
uv tool run --from "huggingface-hub[cli,hf_xet]" hf download `
  nvidia/bigvgan_v2_22khz_80band_256x config.json bigvgan_generator.pt `
  --revision d7b6990ac772ed0ebd93f814912b0027629a7978 `
  --local-dir D:\TTSsystem\external\index-tts\checkpoints\hf_cache\pinned\bigvgan
```

worker 的显式 `aux_paths` 固定映射为 `pinned\w2v_bert`、`pinned\maskgct\semantic_codec\model.safetensors`、`pinned\campplus\campplus_cn_common.bin`、`pinned\bigvgan`。`index-pip-requirements.lock.txt` 是完整可消费锁：首次 `-WriteInitialEnvLocks` 在一次性 bootstrap 中从 pinned Index `uv.lock` 的 `uv export --frozen --no-dev --no-emit-project` 结果加 `workers\indextts2\requirements.txt` roots，经 `uv pip compile --generate-hashes` 生成；必须锁住全部传递依赖，不能只锁 FastAPI/Pydantic/Uvicorn direct pins。候选验证后删除 bootstrap，再仅用 `uv pip sync --require-hashes` clean rebuild 正式 venv。项目 wheel仍 `--no-deps` 安装。只有该显式 flag 且 tracked locks 尚不存在时，才原子发布 requirements lock 与规范化 `index-pip-freeze.txt`；默认运行只能重新生成 candidate/采样 live inventory 并 byte-compare，不得改锁。`-VerifyDisposableRebuild` 在临时目录只消费 requirements lock，比较 freeze 和关键 import 后删除。第二次运行不得改变 Git HEAD、环境锁或已验证 checkpoint：

```powershell
uv run --project D:\TTSsystem\external\index-tts tools/gpu_check.py
```

- [ ] **Step 5: 实现幂等、固定下载 revision 的 setup-gpt-sovits.ps1**

该脚本使用独立的 Miniforge/Conda Python 3.11 环境。不得直接让官方 `install.ps1` 从可变 `main/master` 下载权重；所有模型资产从 `engines.lock.yaml` 的固定 revision 下载，环境只允许由 tracked lock 重建。

环境锁分为：

```text
gsv-conda-explicit.txt             # conda list --explicit，可直接 conda create --file
gsv-pip-requirements.lock.txt      # 全部 pip transitive pins + artifact hashes，可直接 uv pip sync
gsv-pip-freeze.txt                 # live inventory 的规范化验收快照
```

`gsv-pip-requirements.lock.txt` 的生成输入必须引用 pinned checkout 的 `requirements.txt` 和 `extra-req.txt`，并覆盖固定 PyTorch family：`torch==2.7.0+cu128`、`torchaudio==2.7.0+cu128`、`torchcodec==0.4.0`。TorchCodec 官方兼容矩阵明确 0.4 对应 PyTorch 2.7；resolver 输出若带 `+cu128` local suffix，lock 必须保存完整解析版本和 hash。upstream 的裸 `torchaudio` 不能改变这些 pins。`extra-req.txt` 中官方要求 `--no-deps` 的 direct package 要在 lock metadata 中标记并以精确版本单独安装，不能重新解析其 dependency graph。

首次写锁只能显式执行 `setup-gpt-sovits.ps1 -WriteInitialEnvLocks`。脚本在一次性 bootstrap prefix 中从 pinned source 解析，使用 `uv pip compile --generate-hashes` 写临时 candidate；验证 candidate 无范围约束、无可变 VCS/URL、torch family 精确后，才原子发布三份 tracked lock。随后必须删除 bootstrap prefix，再严格从新锁创建正式环境；也就是说首次成功本身就包含一次 clean rebuild。没有该 flag 时，缺少任一 tracked lock立即失败，绝不能解析或改写。

默认重建的核心命令形态固定为：

```powershell
if (-not (Test-Path D:\TTSsystem\external\GPT-SoVITS\.git)) {
  git clone https://github.com/RVC-Boss/GPT-SoVITS.git D:\TTSsystem\external\GPT-SoVITS
}
git -C D:\TTSsystem\external\GPT-SoVITS checkout d523079fc05d9a8028d6085bffe4a2757c32abb6
$CondaExe = (Get-Command conda -ErrorAction Stop).Source
if (-not (Test-Path D:\TTSsystem\external\GPT-SoVITS\.conda\python.exe)) {
  & $CondaExe create -y `
    --prefix D:\TTSsystem\external\GPT-SoVITS\.conda `
    --file D:\TTSsystem\config\env-locks\gsv-conda-explicit.txt
}
$GsvPython = 'D:\TTSsystem\external\GPT-SoVITS\.conda\python.exe'
uv pip sync --python $GsvPython --require-hashes `
  D:\TTSsystem\config\env-locks\gsv-pip-requirements.lock.txt
```

再用 `hf download XXXXRT/GPT-SoVITS-Pretrained` 和固定 revision `4fae8ec36d3d0373864e580b5d8acfba8da29630` 下载 `pretrained_models.zip`、`G2PWModel.zip`、`nltk_data.zip`、`open_jtalk_dic_utf_8-1.11.tar.gz` 到 `runtime\downloads\gsv`。`pretrained_models.zip` 的 SHA-256 必须等于 `82881ee064a0a49c84160908fd08e4dd0c8946e32567ff8df1ad4dad4c358793` 才可解压；镜像仅在字节哈希相同的情况下可用。其余归档和所有解压后的必要文件进入下一步的 checkpoint lock。

已有 repo/env/资产时必须核对 remote、精确 HEAD、Python 版本、archive/hash 和目标文件后复用，不得再次 clone/`conda create`/安装/覆盖解压。每次运行都把 live `conda list --explicit` 和 `python -m pip freeze --all` 规范化到临时文件，要求分别与 tracked explicit/freeze byte-identical；不一致即失败，不自动“修复”或重写 lock。`-VerifyDisposableRebuild` 还要在另一个临时 prefix 只消费两份 install lock，比较 live inventory/关键 import 后删除，以供 Gate C 使用。

脚本启动前若 `conda` 不存在，必须以非零退出并输出安装命令：

```powershell
winget install -e --id CondaForge.Miniforge3 --scope user
```

不得静默安装系统组件。脚本完成后验证：

```powershell
& D:\TTSsystem\external\GPT-SoVITS\.conda\python.exe -c `
  "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda)"
```

- [ ] **Step 6: 生成一次 checkpoint allowlist，此后只允许验证**

`lock-engine-assets.ps1 -WriteInitialLock` 只允许在 pinned source/model revision、archive hash、GPU import 和最小模型加载全部通过后第一次执行。它递归枚举：

- Index `checkpoints` 中实际由 `config.yaml` 与四个显式 pinned `aux_paths` 使用的全部普通文件；
- GSV `tts_infer.yaml`、v2 t2s/vits 权重、Chinese BERT、HuBERT、G2PW、NLTK/OpenJTalk 中实际使用的全部普通文件。

对每个文件保存 engine-relative POSIX path、size 和 SHA-256，并保存两个 model source revisions、GSV archive SHA-256。结果按 path 排序写入被 Git 跟踪的 `config\checkpoints.lock.yaml`。没有 `-WriteInitialLock` 时脚本只能验证，不得重写；缺失、新增或哈希不匹配均非零退出。正式交付的 lock 不能有空 asset 数组。

`runtime\fingerprints.py` 按 engine 生成固定 `EngineFingerprint`，不能把所有锁塞进一个形状不明的 dict。共同输入是 `engines.lock.yaml` 与 `checkpoints.lock.yaml`；Index 的 `environment_lock_sha256` 是 canonical bundle(`index-pip-requirements.lock.txt`, `index-pip-freeze.txt`) 的 SHA-256，GSV 对三份 `gsv-*` lock 做同样计算。bundle 算法固定为：按 basename 排序，对每个文件追加 `basename UTF-8 + NUL + lowercase file SHA-256 + LF` 后整体 SHA-256，绝不拼绝对路径。`runtime_config_sha256` 分别对应 Index `config.yaml` 和 GSV `tts_infer.yaml`。control 另输出 `profile=runtime`、根 `uv.lock`、`control-runtime-requirements.lock.txt`、`control-runtime-freeze.txt` digest。doctor 必须实时重新采样 control/Index/GSV inventory，与相应 tracked freeze byte-compare，并输出 `environment_fingerprints.control/index/gpt_sovits`。真实 clients 构造时注入对应 engine 的同一 `EngineFingerprint`；worker health、manifest 和 audit 记录同一对象，禁止 adapter 自行猜测当前权重或环境。

- [ ] **Step 7: 实现生命周期感知 doctor**

真实 doctor 必须返回：

```text
mode == real
control PID、instance_id、audit path
control/index/gsv 的 Python executable 与版本
control/index/gsv 的 live environment digest 与 tracked lock digest
两个上游 git HEAD
Index checkpoint/config SHA-256
GSV t2s/vits/config SHA-256
CUDA available、GPU name、GPU UUID
两个 worker health
engine_lifecycle
queue max_concurrency == 1
```

三个规范化 Python executable 必须两两不同且都为 3.11。任何必需字段为空、commit/model revision/checkpoint/env lock 不匹配、live inventory 不同或 CUDA 不可用时 doctor 非 0。worker 判定按生命周期：

- `resident`：Index 与 GSV 必须同时 `ready`；
- `exclusive_process`：允许零或一个 `ready`，其余必须是 `stopped_expected`；同时两套配置、解释器、源码与 checkpoint 必须已通过 launch preflight。Gate G 的两次真实任务再证明两套 worker 各自实际达到 ready。

- [ ] **Step 8: 实现动态 PID registry 的 start/stop**

稳定签名：

```powershell
scripts\start.ps1 -Config CONFIG_YAML_ABSOLUTE [-PythonExecutable PYTHON_EXE_ABSOLUTE] -Json
scripts\stop.ps1 -RunFile PROCESSES_JSON_ABSOLUTE -ReceiptPath RECEIPT_JSON_ABSOLUTE -Json
```

`start.ps1` 启动前若 run file 指向仍存活且 create-time 匹配的 control，必须拒绝第二实例；陈旧 run file 只有确认其中所有 PID 已死后才可归档。脚本只用隐藏窗口启动一个 control process，然后等待 `/api/v1/health` 到 ready/degraded terminal 状态或启动超时；health 中的 `control.pid` 必须等于本次 `Start-Process` PID，`instance_id` 必须非空且与 run file 相同，防止误把端口上遗留的旧服务当作本次实例。supervisor 自己注册 control 和每次变化的 worker PID。stdout `-Json` 只返回一个包含 `control_url`、`control_pid`、`control_create_time`、`instance_id`、`audit_log`、`run_file` 的 JSON。启动、readiness、JSON 输出任一步失败时，脚本必须清理本次 PID/子孙后才非零退出。

`stop.ps1` 读取当前 run file，先以 6 秒客户端 timeout POST `/api/v1/control/shutdown`；无论 timeout、非 2xx 或 control 提前退出，都立即重新读取最新 registry，并只对 PID/create-time 匹配的当前记录执行 process-tree fallback。整个脚本从 monotonic start 到所有 verify 最多 10 秒；超过即非零退出并保留 run file/receipt 供诊断。它必须先累积本次见过的 root/child PID、create-time、停止方式与最终 alive=false 校验，原子写入 `-ReceiptPath`，然后才删除 run file；`-Json` stdout 返回同一 receipt。禁止假设启动时总有三个 PID，也禁止按 `python.exe` 或端口模糊批量查杀。

`stop-receipt.json` schema 固定为：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["schema_version", "instance_id", "run_file", "started_at_utc", "finished_at_utc", "elapsed_seconds", "shutdown_http", "observed_processes", "run_file_deleted", "status"],
  "properties": {
    "schema_version": {"const": 1},
    "instance_id": {"type": "string", "format": "uuid"},
    "run_file": {"type": "string", "minLength": 3},
    "started_at_utc": {"type": "string", "format": "date-time"},
    "finished_at_utc": {"type": "string", "format": "date-time"},
    "elapsed_seconds": {"type": "number", "minimum": 0, "maximum": 10},
    "shutdown_http": {
      "type": "object",
      "required": ["attempted", "timeout_seconds", "outcome", "status_code"],
      "properties": {
        "attempted": {"type": "boolean"},
        "timeout_seconds": {"const": 6},
        "outcome": {"enum": ["completed", "timeout", "connection_error", "not_attempted"]},
        "status_code": {"type": ["integer", "null"], "minimum": 100, "maximum": 599}
      },
      "additionalProperties": false
    },
    "observed_processes": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["role", "pid", "create_time", "parent_pid", "stop_method", "verified_exited", "verified_at_utc"],
        "properties": {
          "role": {"enum": ["control", "indextts", "gpt_sovits", "child"]},
          "pid": {"type": "integer", "minimum": 1},
          "create_time": {"type": "number", "exclusiveMinimum": 0},
          "parent_pid": {"type": ["integer", "null"], "minimum": 1},
          "stop_method": {"enum": ["graceful", "terminate", "kill", "already_exited"]},
          "verified_exited": {"const": true},
          "verified_at_utc": {"type": "string", "format": "date-time"}
        },
        "additionalProperties": false
      }
    },
    "run_file_deleted": {"const": true},
    "status": {"const": "stopped"}
  },
  "additionalProperties": false
}
```

字段不可缺省；`outcome != completed` 时 `status_code` 必须为 null，completed 时必须是实际 HTTP status。`status=stopped` 只在所有观察到的 PID/create-time 均 verified exited、`elapsed_seconds <= 10` 且 run file 删除后允许。process tests 必须覆盖挂起 300 秒的 active request、shutdown HTTP timeout、陈旧 PID 重用保护和 receipt 坏样本。

- [ ] **Step 9: 双跑幂等验证、测试、提交**

```powershell
pwsh -NoProfile -File scripts/setup-control.ps1 -WriteInitialEnvLocks
pwsh -NoProfile -File scripts/setup-control.ps1
pwsh -NoProfile -File scripts/setup-indextts.ps1 -WriteInitialEnvLocks -VerifyDisposableRebuild
pwsh -NoProfile -File scripts/setup-indextts.ps1
pwsh -NoProfile -File scripts/setup-gpt-sovits.ps1 -WriteInitialEnvLocks -VerifyDisposableRebuild
pwsh -NoProfile -File scripts/setup-gpt-sovits.ps1
uv run pytest tests/unit/test_fingerprints.py tests/contract/test_doctor.py tests/process/test_start_stop_scripts.py -q
uv run ruff check .
uv run mypy src/voice_pipeline workers
git add config scripts src/voice_pipeline/runtime tests
git commit -m "build: add pinned engine setup and diagnostics"
```

Expected: 全部非 GPU 测试退出码 0；在模型资产可用的开发机上 clean rebuild 与第二次 engine setup 都不改变 tracked lock/env snapshot。资产缺失时明确列出缺项并把 GPU 交付标为 BLOCKED，不能生成空 lock。

---

### Task 10: 完成跨进程 CPU 集成、故障矩阵和 GPU 互斥测试

**Files:**
- Create: `D:\TTSsystem\tests\fixtures\fake_engine_server.py`
- Create: `D:\TTSsystem\tests\process\test_process_isolation.py`
- Create: `D:\TTSsystem\tests\integration_cpu\test_failure_matrix.py`
- Create: `D:\TTSsystem\tests\integration_cpu\test_multi_cli_gpu_mutex.py`
- Create: `D:\TTSsystem\tests\integration_cpu\test_atomic_outputs.py`

**Interfaces:**
- Consumes: packaged CLI and control plane.
- Proves: separate processes/interpreters, no bypass, strict order, fault recovery, cross-CLI serialization.
- Uses: only public `mode=external_test` HTTP contracts; no test-name branch in production code.

- [ ] **Step 1: 构建两个零依赖动态 fake engine server 与三个独立 venv**

`tests\fixtures\fake_engine_server.py` 只能使用 Python 标准库，分别模拟 Index worker 和官方 GSV `/tts`。测试 session 在临时目录精确执行：

```powershell
uv build --wheel --out-dir $Temp\dist
uv venv $Temp\control-env --python 3.11
uv pip install --python $Temp\control-env\Scripts\python.exe $Wheel
uv venv $Temp\index-env --python 3.11
uv venv $Temp\gsv-env --python 3.11
```

用后两个不同解释器启动同一个 fixture script，参数 `--engine indextts|gpt_sovits --host 127.0.0.1 --port 0 --ready-file ...`；server 绑定随机端口后原子写 ready file。生成临时 `external_test` YAML，把 control 端口、两个随机 base URL、三个绝对解释器路径、1–2 秒 timeout 和本次随机 challenge fingerprint 写入；两个 fake health/Index response 必须 echo 相同 fingerprint。再用 `control-env` 启动已安装 wheel。不得用根 `.venv` 充当这三个进程中的任何一个。

fake server 必须使用标准库 `ThreadingHTTPServer`，让 synth 挂起时 `/__control/abort` 仍能由另一线程进入。每个 server 用 `threading.Lock` 保护 active counter 与当前 request 的 `threading.Event` cancellation flag；`POST /__control/abort` 设置 flag，等待该 engine 的 active request 归零后才 200，并返回 `{"active_inference":0,"fingerprint":...}`。产品的 `ExternalEngineRuntime.abort_engine()` 核对该响应和 health；这让 CPU 黑盒可以验证与真实 supervisor 相同的“远端 active inference 已结束后才释放 queue”顺序，而不要求结束测试拥有的 server 进程。

fake server 必须由测试启动在随机 loopback 端口，并记录到 JSONL：

```text
pid
sys.executable
request_id
engine
request body
monotonic_enter
monotonic_exit
reference SHA-256 seen by GSV
```

两个 fake engine 各写自己的 append+flush JSONL，不尝试跨进程共享 Python counter。harness 在测试结束后合并 `monotonic_enter/exit` 区间，离线计算全局 overlap/max active；任意两个成功或被 abort 前的 GPU 区间重叠即失败。响应 WAV 的频率由测试运行时生成的随机 challenge 决定，不得读取仓库内固定 WAV。

- [ ] **Step 2: 写完整故障矩阵**

每一行都必须是独立测试：

| 注入 | 断言 |
|---|---|
| 非法 emotion vector | 两引擎调用数均为 0 |
| Index HTTP 500 | `INDEX_ENGINE_ERROR`；GSV 调用数 0 |
| Index 200 但无文件 | `INVALID_AUDIO`；GSV 调用数 0 |
| Index 返回文本伪装 WAV | `INVALID_AUDIO` |
| Index 超时 | `INDEX_TIMEOUT`；abort 已确认、旧 interval 结束后才允许下一任务 |
| Index 参考 2.9 秒 | `REFERENCE_DURATION_OUT_OF_RANGE`；GSV 调用数 0 |
| Index 参考 9.1 秒 | `REFERENCE_DURATION_OUT_OF_RANGE`；GSV 调用数 0 |
| GSV HTTP 500 | 参考保留；目标不存在 |
| GSV 超时 | 参考保留；目标不存在；abort 已确认后才释放 queue |
| GSV 损坏 WAV | `INVALID_AUDIO`；目标不发布 |
| 输出目标预写 sentinel | 失败时 sentinel SHA-256 不变 |
| 相同 `request_id` 并发提交 | `job_id`/目录不同且均不覆盖 |
| 排队超时 | wall-clock 到期、factory/引擎调用数 0、job 进入 failed |
| 已确认清理的引擎失败后下一任务 | 下一任务成功 |
| abort 无法确认 | queue 进入 poisoned/degraded；pending/new factory 调用数 0，未经 verified recovery 不恢复 |
| post-dispatch reset/截断流/cancellation | 必须 abort 并等旧 interval 结束；connect-before-dispatch/完整 HTTP 500 不误 abort |
| 控制面关闭且 active synth 挂起 300 秒 | 拒绝新任务、abort active、10 秒内结束 control/受管 PID 并生成合规 stop receipt |

- [ ] **Step 3: 写跨 CLI 进程互斥测试**

同时启动 6 个独立 CLI 子进程提交 `reference`、`gsv`、`segment` 混合任务。fake 引擎每次占用临界区 300–600ms。断言：

```text
max_active_observed == 1
所有成功任务的 GPU 区间不相交
health.active_count == 0
health.queued_count == 0
一个任务失败后后续任务仍成功
```

不能只在一个 pytest 进程内调用 `asyncio.gather()`；必须实际启动多个 CLI 进程。

- [ ] **Step 4: 写进程和解释器隔离测试**

断言 control、Index fake、GSV fake：

- PID 三者不同；
- `sys.executable` 三者路径不同；
- 所有端口只监听 `127.0.0.1`；
- CLI 服务不可达时没有任何引擎请求；
- CLI 从仓库外工作目录运行仍成功；
- 中文和空格路径完整传递。
- control/Index/GSV 的三个规范化解释器路径两两不同且均报告 Python 3.11；
- `external_test` 没有启动任何真实模型，也没有绕过 HTTP adapter。

- [ ] **Step 5: 运行 CPU 全套与覆盖率**

```powershell
uv run pytest `
  tests/unit `
  tests/contract `
  tests/integration_cpu `
  tests/process `
  -m "not gpu" `
  -vv `
  -W error `
  --cov=voice_pipeline `
  --cov=workers.indextts2 `
  --cov-branch `
  --cov-fail-under=85
```

Expected: 零失败、零 error、零 xfail、零 skip；分支覆盖率至少 85%。

- [ ] **Step 6: 提交**

```powershell
git add tests
git commit -m "test: verify cross-process gpu serialization"
```

---

### Task 11: 配置真实 GPU 黄金样例和可审计运行证据

**Files:**
- Create: `D:\TTSsystem\testdata\golden\zh-ja-001.template.json`
- Create: `D:\TTSsystem\testdata\golden\zh-en-001.template.json`
- Create: `D:\TTSsystem\tests\gpu\conftest.py`
- Create: `D:\TTSsystem\tests\gpu\test_real_indextts.py`
- Create: `D:\TTSsystem\tests\gpu\test_real_gpt_sovits.py`
- Create: `D:\TTSsystem\tests\gpu\test_real_cross_language.py`
- Create: `D:\TTSsystem\tests\gpu\test_residency_probe.py`
- Create: `D:\TTSsystem\scripts\probe-engine-lifecycle.ps1`
- Create: `D:\TTSsystem\docs\batch-1-gpu-runbook.md`

**Interfaces:**
- Produces: two semantic golden templates, a gitignored mapping to the user's actually verified cases, and real-GPU pytest gates.
- Produces: run manifest with commit, model fingerprints, request snapshot, audio metrics and GPU timings.
- Does not produce: a pre-filled subjective PASS.

- [ ] **Step 1: 定义不伪造本地资产的中到日和中到英模板**

`zh-ja-001.template.json`：

```json
{
  "schema_version": 1,
  "case_id": "zh-ja-001",
  "asset_key": "user_verified_primary",
  "case_data_key": "user_verified_zh_ja",
  "target_language": "ja"
}
```

`zh-en-001.template.json`：

```json
{
  "schema_version": 1,
  "case_id": "zh-en-001",
  "asset_key": "user_verified_primary",
  "case_data_key": "user_verified_zh_en",
  "target_language": "en"
}
```

真实数据只放在 Git 忽略的 `D:\TTSsystem\config\golden-assets.local.yaml`。schema 固定要求：

- `schema_version: 1`；
- `assets.user_verified_primary.base_voice_path`：用户实际测试所用绝对 WAV 路径；
- `cases.user_verified_zh_ja` 与 `cases.user_verified_zh_en`：各自包含实际 `ref_text_cn`、合法 8 维 `emotion_vector`、目标文本、`seed`、`speed_factor`；
- case 的语言必须与 template 相符。

GPU fixture 合并 template 与 local mapping，生成带随机 `request_id` 的完整 `SegmentSynthesisRequest` 到本次 evidence 目录。若 mapping、任一字段或音频缺失，黄金门为 **BLOCKED**，绝不能改用官方 `voice_01.wav` 冒充用户已验证资产。官方样例如需保留，只能命名 `developer-smoke`，不计入正式黄金验收。

自动阈值固定在 GPU 测试代码中：参考时长 `3.0..10.0` 秒、目标时长大于 0.5 秒、RMS 高于 `-50 dBFS`。

- [ ] **Step 2: 写真实 GPU 测试**

GPU 测试只有在：

```powershell
$env:VOICE_PIPELINE_RUN_GPU_TESTS='1'
$env:VOICE_PIPELINE_CONFIG='D:\TTSsystem\config\acceptance.gpu.local.yaml'
```

时运行。若变量已设置但模型、配置或 CUDA 缺失，测试必须 FAIL，不能 skip 或切换 fake。测试独立探测：

- Index 真实输出可解码、非静音、`3..10` 秒；
- 日语与英语目标音频可解码、非静音、有限数值；
- 两个目标输出 SHA-256 不同；
- manifest 的 reference SHA-256 与 GSV adapter 日志记录一致；
- doctor 明确 `mode=real`；
- 日志无 OOM、traceback 或 fake fallback。

此外生成一个动态挑战：验收时随机选择一条短日语或英语目标句并随机 `request_id`，通过同一真实 HTTP 链路合成。断言 adapter audit log 含该动态文本的 SHA-256（不写明文）、实际官方 GSV PID、模型 fingerprint 和 reference SHA-256，且输出 SHA-256 与两份固定黄金不同；不能用硬编码黄金 WAV 通过。

- [ ] **Step 3: 在正式 GPU 套件之前实现独立显存生命周期探针**

`probe-engine-lifecycle.ps1 -BaseConfig ... -EvidenceDir ... -OutputConfig ... -Json` 必须先按 **BaseConfig 所在目录** 解析全部 path 字段，再把 runtime/lock/repo/interpreter 等路径物化为绝对路径后写临时 `resident-candidate.yaml`；不能把含相对路径的 YAML 原样复制到 evidence 目录。随后强制 `engine_lifecycle: resident`，在独立进程运行以下探针并始终 stop：

1. 用 candidate 配置启动 control，要求两个 worker 同时 ready；
2. 预热 Index，再预热 GSV；
3. 连续运行两次完整 Index → GSV；
4. 记录 `nvidia-smi` 的 idle、Index peak、GSV peak、combined peak；
5. 确认第二轮没有模型重新加载日志；
6. 保存 candidate start/doctor/audit/log/stop receipt。

输出 `lifecycle-decision.json` 必须符合固定 schema：

```json
{
  "schema_version": 1,
  "status": "resident_supported",
  "effective_lifecycle": "resident",
  "gpu": {"uuid": "GPU-...", "name": "NVIDIA GeForce RTX 5080", "total_mib": 16303},
  "memory_mib": {
    "idle": 1000,
    "index_peak": 6000,
    "gsv_peak": 7000,
    "combined_peak": 12000,
    "required_reserve": 1024,
    "margin": 3279
  },
  "classification": {
    "kind": "none",
    "oom_detected": false,
    "rule": "combined_peak + required_reserve <= total_mib",
    "source_log_sha256": []
  },
  "candidate_processes": [
    {"role": "control", "pid": 123, "create_time": 1786000000.0}
  ],
  "stop_receipt_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "evidence_paths": ["absolute path"]
}
```

字段及类型固定且不得缺省；所有显存单位都是整数 MiB。`status` 枚举为 `resident_supported | exclusive_required | probe_failed`；`effective_lifecycle` 为 `resident | exclusive_process | null`；`classification.kind` 为 `none | cuda_oom | insufficient_margin | probe_error`。仅有带日志 SHA-256 的明确 CUDA OOM，或 `combined_peak + required_reserve > total_mib` 的数值证据，才能令 status=`exclusive_required`；配置/权重/hash/未知退出只能 `probe_failed`。所有 candidate PID/create-time 必须出现在所引用 stop receipt 且 verified exited。`resident_supported` 时 `OutputConfig` 保持 resident；`exclusive_required` 时只修改 evidence 下的临时有效配置为：

```yaml
engine_lifecycle: exclusive_process
```

探针完全结束并确认 candidate 所有 PID 退出后，才用 `OutputConfig` 新启一个正式服务并运行全部真实 GPU 测试。`test_residency_probe.py` 标记为 `gpu_residency`，只由探针调用；正式套件使用 `-m "gpu and not gpu_residency"`，因此不会在已经启动的最终配置中再次探测或触发 OOM。最终配置必须从头执行两条黄金和动态挑战；代码保留两个模式，本机选择和证据写入 manifest。

在 `exclusive_process` 下不要求两个 worker 同时 ready；每轮日志必须分别证明 Index ready → 推理 → 退出，以及 GSV ready → 推理，且 PID registry 随切换更新。

- [ ] **Step 4: 记录但不伪造人工听感**

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

开发智能体创建的 `listening-review.json` 只能是：

```json
{
  "status": "pending_user_review",
  "reviewer": null,
  "scores": null,
  "blocking_issue": null
}
```

最终分数只能由主智能体展示音频、用户实际试听反馈后写入。

- [ ] **Step 5: 执行开发者 GPU 冒烟并提交测试定义**

```powershell
$env:VOICE_PIPELINE_RUN_GPU_TESTS='1'
pwsh -NoProfile -File scripts/probe-engine-lifecycle.ps1 `
  -BaseConfig 'D:\TTSsystem\config\acceptance.gpu.local.yaml' `
  -EvidenceDir 'D:\TTSsystem\runtime\developer-gpu\lifecycle' `
  -OutputConfig 'D:\TTSsystem\runtime\developer-gpu\effective.gpu.yaml' -Json
$env:VOICE_PIPELINE_CONFIG='D:\TTSsystem\runtime\developer-gpu\effective.gpu.yaml'
uv run pytest tests/gpu -vv -m "gpu and not gpu_residency"
```

Expected: 在模型完整时全部 PASS；模型资产缺失时报告具体缺失路径，交付状态为 BLOCKED，不能用 fake 代替。

```powershell
git add testdata/golden tests/gpu docs/batch-1-gpu-runbook.md
git commit -m "test: add real gpu cross-language golden gates"
```

---

### Task 12: 完成开发者交付包并停止在验收边界

**Files:**
- Modify: `D:\TTSsystem\README.md`
- Create at run time, Git ignored: `D:\TTSsystem\runtime\handoff\batch1-developer-report.json`

**Interfaces:**
- Produces: clean commit and machine-readable handoff report.
- Does not produce: final acceptance decision.

- [ ] **Step 1: 完成 README**

README 必须包含：

- 三环境拓扑；
- engine pins；
- fake 模式首次运行命令；
-真实模式 setup/start/doctor 命令；
- 五个 CLI 示例；
- job 输出目录；
- 错误码；
- 测试分层；
- `resident`/`exclusive_process` 选择方式；
- 明确列出批次 1 非目标。

- [ ] **Step 2: 运行最终开发者检查**

```powershell
uv sync --frozen --extra dev --python 3.11
uv run python -m compileall -q src workers
uv run ruff format --check .
uv run ruff check .
uv run mypy src/voice_pipeline workers
uv run pytest tests/unit tests/contract tests/integration_cpu tests/process `
  -m "not gpu" -vv -W error `
  --cov=voice_pipeline --cov=workers.indextts2 --cov-branch --cov-fail-under=85
uv build --wheel --out-dir runtime/dist
git status --short
```

Expected: 所有检查通过；提交前 `git status` 只包含预期 README 或测试修改。

- [ ] **Step 3: 创建最终 tracked commit 并冻结 SHA**

```powershell
git add README.md src workers scripts tests testdata docs pyproject.toml uv.lock config
git commit -m "docs: complete batch one handoff"
$FinalSha = git rev-parse HEAD
$Dirty = git status --short
if ($Dirty) { throw "working tree is not clean after final commit`n$Dirty" }
```

此后不得再修改 tracked 文件。

- [ ] **Step 4: 针对最终 SHA 生成 Git-ignored 开发者报告**

`batch1-developer-report.json` 必须通过以下 JSON Schema 校验，并填入本次运行的真实值：

```json
{
  "type": "object",
  "required": [
    "schema_version",
    "commit_sha",
    "control_python",
    "index_python",
    "gsv_python",
    "engine_lifecycle",
    "index_revision",
    "gsv_revision",
    "cpu_test_summary",
    "gpu_test_summary",
    "gpu_config_path",
    "golden_output_dirs",
    "known_failures"
  ],
  "properties": {
    "schema_version": {"const": 1},
    "commit_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
    "control_python": {"type": "string", "minLength": 3},
    "index_python": {"type": "string", "minLength": 3},
    "gsv_python": {"type": "string", "minLength": 3},
    "engine_lifecycle": {"enum": ["resident", "exclusive_process"]},
    "index_revision": {"const": "90ca4d608209584bad3a5bd5becc0b80c146e60f"},
    "gsv_revision": {"const": "d523079fc05d9a8028d6085bffe4a2757c32abb6"},
    "cpu_test_summary": {"type": "object"},
    "gpu_test_summary": {"type": "object"},
    "gpu_config_path": {
      "oneOf": [
        {"type": "string", "minLength": 3},
        {"type": "null"}
      ]
    },
    "golden_output_dirs": {"type": "array", "items": {"type": "string"}},
    "known_failures": {"type": "array", "items": {"type": "string"}}
  },
  "additionalProperties": false
}
```

生成脚本必须在最终 commit 后读取 `$FinalSha`，验证三个解释器两两不同；只有 GPU summary 为 passed 时才要求 GPU 配置和列出的黄金输出目录存在。BLOCKED 时 `gpu_config_path` 可为 null，但 `known_failures` 必须列出缺失的具体 local asset/checkpoint。生成报告后再次确认 Git 干净：

```powershell
$ReportedSha = (Get-Content runtime\handoff\batch1-developer-report.json -Raw |
  ConvertFrom-Json).commit_sha
if ($ReportedSha -ne (git rev-parse HEAD)) { throw 'stale handoff commit_sha' }
$Dirty = git status --short
if ($Dirty) { throw "working tree changed after handoff report`n$Dirty" }
```

Expected: 报告绑定最终 HEAD，`git status --short` 无输出。开发智能体此时只报告 commit SHA、报告路径和已知失败，不能自行宣布“批次 1 已验收通过”。

---

# 由主智能体执行的独立验收流程

以下流程不交给开发智能体签字。用户把开发结果交回当前任务后，由主智能体在 `D:\TTSsystem` 从最终 commit 独立创建 challenge、运行 A–H 并保存证据。开发者日志只作参考。

所有 native command 都必须先捕获原始退出码，再写日志，避免 `Tee-Object` 掩盖失败：

```powershell
function Invoke-NativeChecked {
  param(
    [Parameter(Mandatory)][string]$FilePath,
    [Parameter(Mandatory)][string[]]$ArgumentList,
    [Parameter(Mandatory)][string]$LogPath
  )
  $errorLogPath = "$LogPath.stderr"
  $output = & $FilePath @ArgumentList 2> $errorLogPath
  $exitCode = $LASTEXITCODE
  $output | Set-Content $LogPath
  if ($exitCode -ne 0) {
    throw "$FilePath exited $exitCode; see $LogPath and $errorLogPath"
  }
  return ,$output
}
```

## 验收门 A：冻结交付状态

```powershell
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Set-Location 'D:\TTSsystem'
$Uv = (Get-Command uv -ErrorAction Stop).Source
$RunId = 'batch1-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
$Evidence = Join-Path 'D:\TTSsystem\runtime\acceptance' $RunId
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

$GitStatus = & git status --short
if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
$GitStatus | Set-Content "$Evidence\git-status.txt"
if ($GitStatus) { throw "working tree is dirty`n$GitStatus" }

$CommitOutput = Invoke-NativeChecked git @('rev-parse','HEAD') "$Evidence\commit.txt"
$Commit = (($CommitOutput | Out-String).Trim())
$DeveloperReportPath = 'D:\TTSsystem\runtime\handoff\batch1-developer-report.json'
if (-not (Test-Path -LiteralPath $DeveloperReportPath -PathType Leaf)) {
  throw "developer handoff report missing: $DeveloperReportPath"
}
$DeveloperReport = Get-Content -LiteralPath $DeveloperReportPath -Raw |
  ConvertFrom-Json
if ([string]$DeveloperReport.commit_sha -ne $Commit) {
  throw "handoff SHA $($DeveloperReport.commit_sha) != HEAD $Commit"
}
Copy-Item -LiteralPath $DeveloperReportPath `
  -Destination "$Evidence\developer-report.json"
Invoke-NativeChecked git @('diff','--exit-code') "$Evidence\git-diff.txt" | Out-Null
Invoke-NativeChecked git @('diff','--cached','--exit-code') "$Evidence\git-diff-cached.txt" | Out-Null
$GpuPrereqBlocked = $false
$NvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($NvidiaSmi) {
  $NvidiaBefore = & $NvidiaSmi.Source 2>&1
  $NvidiaCode = $LASTEXITCODE
  $NvidiaBefore | Set-Content "$Evidence\nvidia-before.txt"
  if ($NvidiaCode -ne 0) { $GpuPrereqBlocked = $true }
} else {
  'nvidia-smi not found' | Set-Content "$Evidence\nvidia-before.txt"
  $GpuPrereqBlocked = $true
}
```

**通过标准：** tracked/暂存区干净；`$Commit` 为开发报告中的最终 SHA；所有证据目录固定到该 SHA。

## 验收准备 D0：主智能体独立创建验收 harness

冻结 commit 后，主智能体从零创建 Git 忽略目录：

```text
D:\TTSsystem\.acceptance\batch1_cpu_blackbox\
├── conftest.py
├── fake_engine_http.py
├── test_public_flows.py
├── test_failures.py
├── test_gpu_mutex.py
├── test_harness_self.py
├── verify_junit.py
├── render_golden_request.py
├── probe_gpu_prereqs.py
├── run_lifecycle_probe.py
├── verify_reuse_inventory.py
├── emergency_cleanup.py
└── verify_gpu_run.py
```

该 harness 不复制开发者 fixture，并在创建时生成随机 control/engine ports、随机文本 token、音调 challenge、sentinel bytes 和 request IDs。职责固定为：

- `fake_engine_http.py`：标准库 HTTP fake、独立进程/解释器、JSONL audit、可取消 timeout；
- `verify_junit.py`：解析 JUnit，强制 failures/errors/skipped/xfail 均为 0；可同时解析 coverage XML 并强制 branch-rate `>=0.85`；
- `render_golden_request.py`：`--check-only` 校验 mapping/文件/case；缺本地前置时固定退出 20 并输出 JSON `{"status":"blocked","missing":[...]}`，其他错误非 20；前置齐全时合并 template 与 mapping 并写 evidence request；
- `probe_gpu_prereqs.py`：不 import 产品代码，独立验证三个解释器均为 Python 3.11；仅 Index/GSV 环境要求可 import CUDA/PyTorch，clean control 不要求 Torch。另验证 engine/env/checkpoint lock schema，并逐个核对 lock 资产 size/SHA-256；仅“确实缺文件/解释器/CUDA”退出 20，存在但 hash/revision/env 不匹配则普通非零 FAIL；
- `run_lifecycle_probe.py`：先以 base config 目录解析并绝对化所有路径，再只通过公开 start/doctor/job/stop 接口运行 resident candidate，生成 `lifecycle-decision.json` 和 evidence-only effective config；只有明确 OOM/安全余量不足可回退 exclusive，未知错误非零；
- `verify_reuse_inventory.py`：按固定 YAML schema 检查每个生产模块的 candidates、GitHub 来源、SPDX license、immutable pin、采用/拒绝理由、wrapper boundary 和 lock reference，并交叉核对 engine/dependency locks；
- `emergency_cleanup.py`：run file 丢失/损坏或正常 stop 失败时，先请求 loopback shutdown，再只按已验证 control PID/create-time 递归终止其进程树，输出与正式 stop receipt 同 schema 的 emergency receipt；PID/create-time 不匹配时拒绝操作；
- `verify_gpu_run.py`：独立用 SoundFile/NumPy、JSON、CSV 和日志验证 Gate G 的所有客观标准。

执行并记录：

```powershell
Invoke-NativeChecked git @('check-ignore','-q','.acceptance/probe.txt') `
  "$Evidence\acceptance-ignore.txt" | Out-Null
Get-ChildItem 'D:\TTSsystem\.acceptance\batch1_cpu_blackbox' -File |
  Sort-Object Name |
  Get-FileHash -Algorithm SHA256 |
  ConvertTo-Json -Depth 3 |
  Set-Content "$Evidence\acceptance-harness-sha256.json"
```

开发智能体在其 commit 中看不到本次随机 challenge。

D0 是刻意保留给主智能体的独立测试编写 checkpoint，而不是开发交付中的预置脚本：主智能体必须先完成这些文件并逐项对照上面的职责审阅。`test_harness_self.py` 要用合成 JUnit/coverage、损坏 WAV/manifest、重叠 interval、假 PID receipt、缺失 checkpoint、hash 不匹配、空壳 reuse inventory 和伪造 lifecycle decision 证明：好样本返回 0，缺失 prerequisite 精确返回 20，其余坏样本非 0。由于 clean Python 在 Gate B 才建立，harness self-test 放在 Gate B 末尾执行；未通过不得进入 C–H。

## 验收门 B：干净环境、静态、类型和打包

```powershell
Invoke-NativeChecked $Uv @('sync','--frozen','--extra','dev','--python','3.11') `
  "$Evidence\uv-sync.txt" | Out-Null
Invoke-NativeChecked $Uv @('run','--frozen','python','-m','compileall','-q','src','workers') `
  "$Evidence\compileall.txt" | Out-Null
Invoke-NativeChecked $Uv @('run','--frozen','ruff','format','--check','.') `
  "$Evidence\ruff-format.txt" | Out-Null
Invoke-NativeChecked $Uv @('run','--frozen','ruff','check','.') `
  "$Evidence\ruff-check.txt" | Out-Null
Invoke-NativeChecked $Uv @('run','--frozen','mypy','src/voice_pipeline','workers') `
  "$Evidence\mypy.txt" | Out-Null
Invoke-NativeChecked $Uv @('run','--frozen','pytest','--collect-only','-q') `
  "$Evidence\pytest-collection.txt" | Out-Null
Invoke-NativeChecked $Uv @('build','--wheel','--out-dir',"$Evidence\dist") `
  "$Evidence\wheel-build.txt" | Out-Null

$Wheel = (Get-ChildItem "$Evidence\dist\*.whl" | Select-Object -First 1).FullName
if (-not $Wheel) { throw 'wheel missing' }
$CleanVenv = Join-Path $env:TEMP "$RunId-control"
Invoke-NativeChecked $Uv @('venv',$CleanVenv,'--python','3.11') `
  "$Evidence\clean-venv.txt" | Out-Null
$CleanPython = Join-Path $CleanVenv 'Scripts\python.exe'
$ControlRuntimeLock = 'D:\TTSsystem\config\env-locks\control-runtime-requirements.lock.txt'
Invoke-NativeChecked $Uv @(
  'pip','sync','--python',$CleanPython,'--require-hashes',
  $ControlRuntimeLock
) "$Evidence\control-lock-sync.txt" | Out-Null
Invoke-NativeChecked $Uv @(
  'pip','install','--python',$CleanPython,'--no-cache','--no-deps',$Wheel
) `
  "$Evidence\wheel-install.txt" | Out-Null
Invoke-NativeChecked $Uv @('pip','check','--python',$CleanPython) `
  "$Evidence\pip-check.txt" | Out-Null
Invoke-NativeChecked $CleanPython @(
  'D:\TTSsystem\scripts\export-environment-inventory.py',
  '--output',"$Evidence\control-runtime-freeze.txt"
) "$Evidence\control-inventory-export.log" | Out-Null
$LiveControlFreeze = Get-Content "$Evidence\control-runtime-freeze.txt"
$ExpectedControlFreeze = Get-Content `
  'D:\TTSsystem\config\env-locks\control-runtime-freeze.txt'
if (Compare-Object $ExpectedControlFreeze $LiveControlFreeze) {
  throw 'clean control inventory differs from tracked runtime profile'
}
```

从仓库外执行：

```powershell
$Outside = Join-Path $env:TEMP "$RunId-outside"
New-Item -ItemType Directory -Force -Path $Outside | Out-Null
Push-Location $Outside
try {
  Invoke-NativeChecked $CleanPython @('-c','import voice_pipeline,inspect; print(inspect.getfile(voice_pipeline))') `
    "$Evidence\outside-import.txt" | Out-Null
  Invoke-NativeChecked $CleanPython @('-m','voice_pipeline','--help') `
    "$Evidence\outside-help.txt" | Out-Null
} finally {
  Pop-Location
}
```

扫描器对 `rg` 的“无匹配=1”单独处理：

```powershell
$DebtPattern = '\bTO' + 'DO\b|\bFIX' + 'ME\b|\bNotImplementedError\b|^\s*pass\s*(#.*)?$'
$DebtHits = & rg -n $DebtPattern src workers scripts 2>$null
$DebtCode = $LASTEXITCODE
if ($DebtCode -gt 1) { throw "rg debt scan failed: $DebtCode" }
$DebtHits | Set-Content "$Evidence\debt-scan.txt"
if ($DebtHits) { throw "unfinished production code found`n$DebtHits" }

$BypassHits = & rg -n 'PYTEST_CURRENT_TEST|tests[/\\]|golden.*wav|return.*fixture' `
  src workers scripts 2>$null
$BypassCode = $LASTEXITCODE
if ($BypassCode -gt 1) { throw "rg bypass scan failed: $BypassCode" }
$BypassHits | Set-Content "$Evidence\bypass-scan.txt"
if ($BypassHits) { throw "test bypass found`n$BypassHits" }

$Tracked = & git ls-files
if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed' }
$ForbiddenTracked = $Tracked | Where-Object {
  $_ -match '(^|/)config/.*\.local\.ya?ml$|(^|/)(external|runtime|\.acceptance)/|\.safetensors$|\.ckpt$|\.pth$'
}
$ForbiddenTracked | Set-Content "$Evidence\forbidden-tracked.txt"
if ($ForbiddenTracked) { throw "forbidden tracked files found`n$ForbiddenTracked" }

$StaticDeliverables = @(
  'D:\TTSsystem\config\engines.lock.yaml',
  'D:\TTSsystem\config\open-source-reuse.yaml',
  'D:\TTSsystem\config\env-locks\control-runtime-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\control-runtime-freeze.txt',
  'D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\index-pip-freeze.txt',
  'D:\TTSsystem\config\env-locks\gsv-conda-explicit.txt',
  'D:\TTSsystem\config\env-locks\gsv-pip-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\gsv-pip-freeze.txt',
  'D:\TTSsystem\docs\batch-1-open-source-reuse.md'
)
foreach ($StaticDeliverable in $StaticDeliverables) {
  if (-not (Test-Path -LiteralPath $StaticDeliverable -PathType Leaf)) {
    throw "tracked batch deliverable missing: $StaticDeliverable"
  }
}

$ReuseDoc = 'D:\TTSsystem\docs\batch-1-open-source-reuse.md'
Invoke-NativeChecked $CleanPython @(
  'D:\TTSsystem\.acceptance\batch1_cpu_blackbox\verify_reuse_inventory.py',
  '--inventory','D:\TTSsystem\config\open-source-reuse.yaml',
  '--engine-lock','D:\TTSsystem\config\engines.lock.yaml',
  '--uv-lock','D:\TTSsystem\uv.lock',
  '--rendered-doc',$ReuseDoc
) "$Evidence\reuse-inventory-verified.txt" | Out-Null

Invoke-NativeChecked $Uv @(
  'run','--frozen','pytest',
  'D:\TTSsystem\.acceptance\batch1_cpu_blackbox\test_harness_self.py',
  '-vv',"--junitxml=$Evidence\harness-self.xml"
) "$Evidence\harness-self.log" | Out-Null
Invoke-NativeChecked $CleanPython @(
  'D:\TTSsystem\.acceptance\batch1_cpu_blackbox\verify_junit.py',
  '--junit',"$Evidence\harness-self.xml",'--require-zero-skips'
) "$Evidence\harness-self-verified.txt" | Out-Null
```

**通过标准：** 所有检查成功；wheel 可在仓库外运行；无测试特判、密钥、权重、真实本机配置或未完成实现进入 Git；开源复用清单完整且没有无理由重写上游现成功能；D0 harness self-test 通过。任何主智能体当场修订 harness 都要重新记录 SHA-256 并重跑 self-test。

## 验收门 C：开发者测试重新全量运行

```powershell
Invoke-NativeChecked $Uv @(
  'run','--frozen','pytest',
  'tests/unit','tests/contract','tests/integration_cpu','tests/process',
  '-m','not gpu','-vv','--strict-config','--strict-markers','-W','error',
  '--cov=voice_pipeline','--cov=workers.indextts2','--cov-branch','--cov-fail-under=85',
  "--cov-report=xml:$Evidence\coverage.xml",
  "--junitxml=$Evidence\cpu-tests.xml"
) "$Evidence\cpu-tests.log" | Out-Null

Invoke-NativeChecked $CleanPython @(
  'D:\TTSsystem\.acceptance\batch1_cpu_blackbox\verify_junit.py',
  '--junit',"$Evidence\cpu-tests.xml",
  '--coverage',"$Evidence\coverage.xml",
  '--min-branch-rate','0.85',
  '--require-zero-skips'
) "$Evidence\cpu-tests-verified.txt" | Out-Null
```

**通过标准：** 解析后的 JUnit 零失败、零 error、零 skip/xfail；分支覆盖率至少 85%，不是只相信 pytest 的退出码。

## 验收门 D：动态三进程 CPU 黑盒

```powershell
$env:BATCH1_WHEEL = $Wheel
$env:BATCH1_CLEAN_PYTHON = $CleanPython
$env:BATCH1_EVIDENCE_DIR = $Evidence
$Harness = 'D:\TTSsystem\.acceptance\batch1_cpu_blackbox'

Invoke-NativeChecked $Uv @(
  'run','--frozen','pytest',$Harness,'-vv','--maxfail=1',
  '-k','public_flows or process_isolation',
  "--junitxml=$Evidence\blackbox-public.xml"
) "$Evidence\blackbox-public.log" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\verify_junit.py",'--junit',"$Evidence\blackbox-public.xml",'--require-zero-skips'
) "$Evidence\blackbox-public-verified.txt" | Out-Null
```

黑盒必须证明：

1. control、Index fake、GSV fake PID 不同，三个 Python 3.11 解释器路径两两不同；
2. 所有端口只绑定随机 `127.0.0.1`，mode 为 `external_test`；
3. 控制面不可达时有效 CLI 请求返回 `CONTROL_PLANE_UNAVAILABLE`，两引擎调用数为 0；
4. reference 只调 Index；GSV 只调 GSV；segment 严格 Index 后 GSV；
5. GSV 收到实际 Index WAV SHA-256 和准确 `ref_text_cn`；
6. 独立 GSV 前后 reference WAV/manifest SHA-256 不变；
7. 相同 `request_id` 得到不同 `job_id`/目录；
8. 动态随机输入影响输出，独立 SoundFile 解码，不信产品 metadata；
9. CLI 从仓库外运行且 JSON stdout 恰好一个对象；
10. manifest endpoints、portable manifest 和 request/seed/effective parameters/fingerprints 完整。

## 验收门 E：故障、超时、原子性和进程回收

```powershell
Invoke-NativeChecked $Uv @(
  'run','--frozen','pytest',"$Harness\test_failures.py",'-vv',
  "--junitxml=$Evidence\blackbox-failures.xml"
) "$Evidence\blackbox-failures.log" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\verify_junit.py",'--junit',"$Evidence\blackbox-failures.xml",'--require-zero-skips'
) "$Evidence\blackbox-failures-verified.txt" | Out-Null
```

必须覆盖 Task 10 全矩阵，并额外满足：

- queue timeout 1 秒时最迟 5 秒返回，factory/引擎调用数为 0，job 为 failed；
- engine timeout 后先收到 abort ack、旧 interval 结束，queue 才启动下一项；
- timeout 后 2 秒内无该 fake 推理子进程残留；
- Index 失败绝不调用 GSV；
- GSV 失败保留参考，不发布目标；
- sentinel SHA-256 不变，partial/reservation 无泄漏；
- 异常后的下一任务成功；
- stop 后 10 秒内所有记录 PID/子孙退出。

## 验收门 F：跨 CLI 进程 GPU 互斥

```powershell
Invoke-NativeChecked $Uv @(
  'run','--frozen','pytest',"$Harness\test_gpu_mutex.py",'-vv',
  "--junitxml=$Evidence\blackbox-mutex.xml"
) "$Evidence\blackbox-mutex.log" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\verify_junit.py",'--junit',"$Evidence\blackbox-mutex.xml",'--require-zero-skips'
) "$Evidence\blackbox-mutex-verified.txt" | Out-Null
```

**通过标准：** 6 个独立 CLI 混合请求；`max_active_observed == 1`；所有成功区间不相交；排队 timeout 不进 engine；最后 `active_count == queued_count == 0`；异常后队列继续。

## 验收门 G：真实 RTX 5080 双引擎 GPU

真实配置和用户黄金映射只允许位于：

```text
D:\TTSsystem\config\acceptance.gpu.local.yaml
D:\TTSsystem\config\golden-assets.local.yaml
```

若 `$GpuPrereqBlocked` 为 true，或任一文件、checkpoint lock 对应资产、用户已验证 case 数据缺失，此门记 **BLOCKED** 并列出缺项；不能替换为 fake/官方示例。前置完整时：

```powershell
$GpuBaseConfig = 'D:\TTSsystem\config\acceptance.gpu.local.yaml'
$GpuConfig = $null
$GoldenMap = 'D:\TTSsystem\config\golden-assets.local.yaml'
$GpuDir = Join-Path $Evidence 'gpu'
New-Item -ItemType Directory -Force -Path $GpuDir | Out-Null

$BlockedReasons = @()
if ($GpuPrereqBlocked) { $BlockedReasons += 'nvidia-smi/CUDA prerequisite unavailable' }
foreach ($TrackedRequiredPath in @(
  'D:\TTSsystem\config\engines.lock.yaml',
  'D:\TTSsystem\config\open-source-reuse.yaml',
  'D:\TTSsystem\config\env-locks\control-runtime-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\control-runtime-freeze.txt',
  'D:\TTSsystem\config\env-locks\index-pip-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\index-pip-freeze.txt',
  'D:\TTSsystem\config\env-locks\gsv-conda-explicit.txt',
  'D:\TTSsystem\config\env-locks\gsv-pip-requirements.lock.txt',
  'D:\TTSsystem\config\env-locks\gsv-pip-freeze.txt'
)) {
  if (-not (Test-Path -LiteralPath $TrackedRequiredPath -PathType Leaf)) {
    throw "tracked batch deliverable missing: $TrackedRequiredPath"
  }
}
foreach ($BlockableRequiredPath in @(
  $GpuBaseConfig,
  $GoldenMap,
  'D:\TTSsystem\config\checkpoints.lock.yaml'
)) {
  if (-not (Test-Path -LiteralPath $BlockableRequiredPath -PathType Leaf)) {
    $BlockedReasons += "missing local/GPU asset prerequisite: $BlockableRequiredPath"
  }
}
if (-not $BlockedReasons) {
  $PrereqErr = "$GpuDir\engine-prereq.stderr.txt"
  $Prereq = & $CleanPython "$Harness\probe_gpu_prereqs.py" `
    '--config' $GpuBaseConfig `
    '--engine-lock' 'D:\TTSsystem\config\engines.lock.yaml' `
    '--checkpoint-lock' 'D:\TTSsystem\config\checkpoints.lock.yaml' `
    '--env-lock-dir' 'D:\TTSsystem\config\env-locks' 2> $PrereqErr
  $PrereqCode = $LASTEXITCODE
  $Prereq | Set-Content "$GpuDir\engine-prereq.json"
  if ($PrereqCode -eq 20) {
    $BlockedReasons += (($Prereq | Out-String).Trim())
  } elseif ($PrereqCode -ne 0) {
    throw "engine prerequisite mismatch/checker failure: $PrereqCode"
  }
}
if (-not $BlockedReasons) {
  $GoldenCheckErr = "$GpuDir\golden-prereq.stderr.txt"
  $GoldenCheck = & $CleanPython "$Harness\render_golden_request.py" `
    '--check-only' '--mapping' $GoldenMap 2> $GoldenCheckErr
  $GoldenCheckCode = $LASTEXITCODE
  $GoldenCheck | Set-Content "$GpuDir\golden-prereq.json"
  if ($GoldenCheckCode -eq 20) {
    $BlockedReasons += (($GoldenCheck | Out-String).Trim())
  } elseif ($GoldenCheckCode -ne 0) {
    throw "golden prerequisite checker failed with $GoldenCheckCode"
  }
}
if ($BlockedReasons) {
  [pscustomobject]@{
    status = 'BLOCKED'
    gate = 'G'
    reasons = $BlockedReasons
  } | ConvertTo-Json -Depth 5 | Set-Content "$GpuDir\blocked.json"
  $FinalDisposition = 'BLOCKED'
  return
}

$LifecycleDir = Join-Path $GpuDir 'lifecycle-probe'
$GpuConfig = Join-Path $GpuDir 'effective.gpu.yaml'
New-Item -ItemType Directory -Force -Path $LifecycleDir | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\run_lifecycle_probe.py",
  '--base-config',$GpuBaseConfig,
  '--golden-mapping',$GoldenMap,
  '--control-python',$CleanPython,
  '--evidence',$LifecycleDir,
  '--output-config',$GpuConfig
) "$LifecycleDir\probe.log" | Out-Null
$LifecycleDecision = Get-Content `
  -LiteralPath "$LifecycleDir\lifecycle-decision.json" -Raw | ConvertFrom-Json
if ($LifecycleDecision.status -notin @('resident_supported','exclusive_required')) {
  throw "lifecycle probe did not produce a usable decision"
}
if (-not (Test-Path -LiteralPath $GpuConfig -PathType Leaf)) {
  throw "lifecycle probe did not create effective config"
}

Invoke-NativeChecked $CleanPython @(
  "$Harness\render_golden_request.py",
  '--template','D:\TTSsystem\testdata\golden\zh-ja-001.template.json',
  '--mapping',$GoldenMap,'--output',"$GpuDir\zh-ja-001.request.json"
) "$GpuDir\render-ja.txt" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\render_golden_request.py",
  '--template','D:\TTSsystem\testdata\golden\zh-en-001.template.json',
  '--mapping',$GoldenMap,'--output',"$GpuDir\zh-en-001.request.json"
) "$GpuDir\render-en.txt" | Out-Null

$GpuMonitor = $null
$RunFile = 'D:\TTSsystem\runtime\run\processes.json'
$BaseUrl = $null
$AuditLog = $null
$ControlPid = $null
$ControlCreateTime = $null
$InstanceId = $null

try {
  $StartOutput = Invoke-NativeChecked pwsh @(
    '-NoProfile','-File','D:\TTSsystem\scripts\start.ps1',
    '-Config',$GpuConfig,'-PythonExecutable',$CleanPython,'-Json'
  ) "$GpuDir\start.json"
  $StartInfo = (($StartOutput | Out-String).Trim()) | ConvertFrom-Json
  foreach ($RequiredStartField in @(
    'control_url','control_pid','control_create_time','instance_id','audit_log','run_file'
  )) {
    if ($RequiredStartField -notin $StartInfo.PSObject.Properties.Name) {
      throw "start output missing $RequiredStartField"
    }
  }
  $BaseUrl = [string]$StartInfo.control_url
  $ControlPid = [int]$StartInfo.control_pid
  $ControlCreateTime = [double]$StartInfo.control_create_time
  $InstanceId = [string]$StartInfo.instance_id
  $RunFile = [string]$StartInfo.run_file
  $AuditLog = [string]$StartInfo.audit_log
  if (-not $BaseUrl -or $ControlPid -le 0 -or $ControlCreateTime -le 0 `
      -or -not $InstanceId -or -not $RunFile -or -not $AuditLog) {
    throw 'start output contains empty/invalid fields'
  }

  $GpuMonitor = Start-Process 'nvidia-smi.exe' `
    -ArgumentList '--query-gpu=timestamp,uuid,name,memory.used,utilization.gpu',
                  '--format=csv','-lms','250','-f',"$GpuDir\nvidia-during.csv" `
    -WindowStyle Hidden -PassThru

  Invoke-NativeChecked $CleanPython @(
    '-m','voice_pipeline','doctor','--server',$BaseUrl,'--json'
  ) "$GpuDir\doctor.json" | Out-Null

  $env:VOICE_PIPELINE_RUN_GPU_TESTS = '1'
  $env:VOICE_PIPELINE_CONFIG = $GpuConfig
  $env:VOICE_PIPELINE_SERVER = $BaseUrl
  $env:VOICE_PIPELINE_GOLDEN_ASSETS = $GoldenMap
  $env:VOICE_PIPELINE_EVIDENCE_DIR = $GpuDir
  Invoke-NativeChecked $Uv @(
    'run','--frozen','pytest','tests/gpu','-vv','-m','gpu and not gpu_residency',
    "--junitxml=$GpuDir\gpu-tests.xml"
  ) "$GpuDir\gpu-tests.log" | Out-Null

  Invoke-NativeChecked $CleanPython @(
    '-m','voice_pipeline','synthesize-segment','--server',$BaseUrl,
    '--request',"$GpuDir\zh-ja-001.request.json",
    '--output-dir',"$GpuDir\zh-ja-001",'--json'
  ) "$GpuDir\zh-ja-001-result.json" | Out-Null
  Invoke-NativeChecked $CleanPython @(
    '-m','voice_pipeline','synthesize-segment','--server',$BaseUrl,
    '--request',"$GpuDir\zh-en-001.request.json",
    '--output-dir',"$GpuDir\zh-en-001",'--json'
  ) "$GpuDir\zh-en-001-result.json" | Out-Null

  Invoke-NativeChecked $CleanPython @(
    "$Harness\render_golden_request.py",'--dynamic-challenge',
    '--mapping',$GoldenMap,'--output',"$GpuDir\dynamic.request.json"
  ) "$GpuDir\render-dynamic.txt" | Out-Null
  Invoke-NativeChecked $CleanPython @(
    '-m','voice_pipeline','synthesize-segment','--server',$BaseUrl,
    '--request',"$GpuDir\dynamic.request.json",
    '--output-dir',"$GpuDir\dynamic",'--json'
  ) "$GpuDir\dynamic-result.json" | Out-Null
}
finally {
  Remove-Item Env:VOICE_PIPELINE_RUN_GPU_TESTS -ErrorAction SilentlyContinue
  Remove-Item Env:VOICE_PIPELINE_CONFIG -ErrorAction SilentlyContinue
  Remove-Item Env:VOICE_PIPELINE_SERVER -ErrorAction SilentlyContinue
  Remove-Item Env:VOICE_PIPELINE_GOLDEN_ASSETS -ErrorAction SilentlyContinue
  Remove-Item Env:VOICE_PIPELINE_EVIDENCE_DIR -ErrorAction SilentlyContinue
  if ($GpuMonitor -and -not $GpuMonitor.HasExited) {
    Stop-Process -Id $GpuMonitor.Id -Force
    $GpuMonitor.WaitForExit()
  }
  $NormalStopSucceeded = $false
  $NormalStopError = $null
  try {
    if ($RunFile -and (Test-Path $RunFile)) {
      Copy-Item -LiteralPath $RunFile `
        -Destination "$GpuDir\processes-before-stop.json" -Force
      Invoke-NativeChecked pwsh @(
        '-NoProfile','-File','D:\TTSsystem\scripts\stop.ps1',
        '-RunFile',$RunFile,
        '-ReceiptPath',"$GpuDir\stop-receipt.json",
        '-Json'
      ) "$GpuDir\stop.json" | Out-Null
      $NormalStopSucceeded = $true
    } else {
      $NormalStopError = 'run file missing before stop'
    }
  }
  catch {
    $NormalStopError = $_.Exception.Message
  }
  finally {
    if ($AuditLog -and (Test-Path -LiteralPath $AuditLog -PathType Leaf)) {
      Copy-Item -LiteralPath $AuditLog `
        -Destination "$GpuDir\engine-audit.jsonl" -Force
    }
  }
  if (-not $NormalStopSucceeded) {
    if ($ControlPid -and $ControlCreateTime -and $InstanceId) {
      Invoke-NativeChecked $CleanPython @(
        "$Harness\emergency_cleanup.py",
        '--base-url',$BaseUrl,
        '--pid',[string]$ControlPid,
        '--create-time',[string]$ControlCreateTime,
        '--instance-id',$InstanceId,
        '--receipt',"$GpuDir\emergency-stop-receipt.json"
      ) "$GpuDir\emergency-stop.log" | Out-Null
    }
    throw "normal stop failed; emergency cleanup attempted: $NormalStopError"
  }
}

Invoke-NativeChecked nvidia-smi @() "$GpuDir\nvidia-after.txt" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\verify_junit.py",'--junit',"$GpuDir\gpu-tests.xml",'--require-zero-skips'
) "$GpuDir\gpu-tests-verified.txt" | Out-Null
Invoke-NativeChecked $CleanPython @(
  "$Harness\verify_gpu_run.py",'--evidence',$GpuDir,
  '--engine-lock','D:\TTSsystem\config\engines.lock.yaml',
  '--checkpoint-lock','D:\TTSsystem\config\checkpoints.lock.yaml',
  '--env-lock-dir','D:\TTSsystem\config\env-locks',
  '--effective-config',$GpuConfig,
  '--lifecycle-decision',"$LifecycleDir\lifecycle-decision.json",
  '--expected-gpu','NVIDIA GeForce RTX 5080'
) "$GpuDir\gpu-objective-verification.txt" | Out-Null
```

`verify_gpu_run.py` 必须实际断言：

- doctor `mode=real`、无 fake fallback，control PID/instance_id 与 start/process snapshot 一致，三解释器两两不同且 Python 3.11；
- source/model/checkpoint/env fingerprints 与 lock 一致；
- lifecycle probe 的 candidate 进程全部退出；`resident_supported` 才允许最终 resident，`exclusive_required` 必须有明确 OOM/余量数值证据；最终 `resident` 为两 worker ready，`exclusive_process` 为 lifecycle-aware 状态且 audit 证明两套 worker 各自 ready/推理；
- queue 最大并发 1，真实 GSV PID、动态目标文本 SHA-256 与 reference SHA-256 出现在 `engine-audit.jsonl`；
- 参考为单声道、`3.0..10.0` 秒、RMS `>-50 dBFS`；
- 三个目标可解码、非静音、有限值，削波比例 `<1%`；
- reference/target 与三个 target 之间的 SHA-256 符合预期且动态输出不是固定黄金；
- manifest 的 reference SHA-256 等于 GSV audit，seed/有效参数/fingerprint 完整；
- `nvidia-during.csv` 观察到 RTX 5080 显存或利用率变化；
- 正式运行日志无 OOM、traceback、自动 fake fallback；仅 `exclusive_required` 的隔离 candidate probe 日志允许出现已分类 OOM；
- `stop-receipt.json` 严格符合固定 schema、`elapsed_seconds <= 10`，列出的 root/child PID/create-time 均为 verified exited，run file 已删除；verifier 再独立检查这些 PID 未以相同 create-time 存活。

## 验收门 H：黄金试听与最终签字

主智能体在客观 Gate G 通过后向用户展示：

```text
$Evidence\gpu\zh-ja-001\target.wav
$Evidence\gpu\zh-en-001\target.wav
```

用户分别按 1–5 分确认文本准确、目标语言自然、情绪明显、与中文参考情绪一致，并确认无严重断裂/爆音/吞字/截断。每项至少 4 分且无一票否决才通过；主智能体把实际反馈写入 evidence 下 `listening-review.json`，开发智能体不得预填。

## 最终判定

```text
PASS    = A–H 全部通过
BLOCKED = A–F 通过，但缺本地 CUDA/真实模型资产、由这些资产生成的 checkpoint lock、.local GPU 配置、用户黄金 mapping 或用户试听
FAIL    = 任一已具备前提的门不满足；任何 tracked source/env/reuse lock 缺失或畸形都属于 FAIL
```

源代码或依赖锁改变至少重跑 A–F；engine commit、model revision、checkpoint、黄金音色或模型调用代码改变必须重跑 A–H。只有当前主智能体可以签发最终判定。

---

# 上游契约依据

- IndexTTS2 固定源码：`https://github.com/index-tts/index-tts/tree/90ca4d608209584bad3a5bd5becc0b80c146e60f`
- IndexTTS2 Python 版本与 CUDA 12.8 依赖：`https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/pyproject.toml`
- IndexTTS2 `infer()` 参数：`https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/indextts/infer_v2.py`
- IndexTTS2 上游 auxiliary model 下载逻辑：`https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/indextts/utils/model_download.py`
- IndexTTS2 8 维向量规则：`https://github.com/index-tts/index-tts/blob/90ca4d608209584bad3a5bd5becc0b80c146e60f/docs/cli_v2_usage.md`
- IndexTTS2 模型 revision：`https://huggingface.co/IndexTeam/IndexTTS-2/commit/740dcaff396282ffb241903d150ac011cd4b1ede`
- GPT-SoVITS 固定源码：`https://github.com/RVC-Boss/GPT-SoVITS/tree/d523079fc05d9a8028d6085bffe4a2757c32abb6`
- GPT-SoVITS 官方 `/tts` 契约：`https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/api_v2.py`
- GPT-SoVITS pinned requirements（含裸 `torchaudio`）：`https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/requirements.txt`
- GPT-SoVITS 官方 Windows 安装流程：`https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/install.ps1`
- GPT-SoVITS Windows/Conda 安装与 Python 3.11 + CUDA 12.8 测试环境：`https://github.com/RVC-Boss/GPT-SoVITS/blob/d523079fc05d9a8028d6085bffe4a2757c32abb6/README.md`
- TorchCodec/PyTorch 兼容矩阵（0.4 对应 torch 2.7）：`https://github.com/meta-pytorch/torchcodec#compatibility-with-torch-versions`
- GPT-SoVITS 预训练归档 revision/hash 依据：`https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/commit/4fae8ec36d3d0373864e580b5d8acfba8da29630`
