# Batch 1 开源复用说明

本文档是对 `config/open-source-reuse.yaml` 的人类可读说明，记录批次 1 中每个生产模块的开源复用决策、SPDX 许可证、不可变 pin、wrapper 边界与取舍理由。

## 原则

- 上游官方实现和成熟、维护活跃、许可证兼容的库，一律以固定版本/commit 直接依赖；
- 不复制上游源码改名、不重写已有通用能力；
- 仅允许为项目安全边界自研"薄适配/包装"（worker 外壳、HTTP adapter、原子发布、受管进程生命周期、双引擎编排、单 GPU fail-closed 互斥、reference/manifest 绑定、验收 challenge）。

## 直接复用（reuse）

| 模块 | 来源 | SPDX | pin |
|---|---|---|---|
| IndexTTS2 推理 | github.com/index-tts/index-tts | MIT | `90ca4d608209584bad3a5bd5becc0b80c146e60f` |
| GPT-SoVITS api_v2.py | github.com/RVC-Boss/GPT-SoVITS | MIT | `d523079fc05d9a8028d6085bffe4a2757c32abb6` |
| FastAPI | github.com/fastapi/fastapi | MIT | `>=0.115.2,<1` |
| Uvicorn | github.com/encode/uvicorn | BSD-3-Clause | `>=0.34,<1` |
| HTTPX | github.com/encode/httpx | BSD-3-Clause | `>=0.28,<1` |
| Pydantic | github.com/pydantic/pydantic | MIT | `>=2.10,<3` |
| Typer | github.com/fastapi/typer | MIT | `>=0.15,<1` |
| psutil | github.com/giampaolo/psutil | BSD-3-Clause | `>=6.1,<8` |
| SoundFile | github.com/bastibe/python-soundfile | BSD-3-Clause | `>=0.13,<1` |
| NumPy | github.com/numpy/numpy | BSD-3-Clause | `>=2.1,<3` |
| Hugging Face Hub | github.com/huggingface/huggingface_hub | Apache-2.0 | cli+hf_xet |
| uv | github.com/astral-sh/uv | MIT OR Apache-2.0 | 0.11.24 |
| pytest 生态 | github.com/pytest-dev/pytest 等 | MIT | `>=8.3,<9` 等 |

## 薄包装（thin_wrapper）

| 模块 | 边界 | 说明 |
|---|---|---|
| Index HTTP worker | `workers/indextts2/*` | 仅做 HTTP 外壳、参数校验、路径限制、指纹握手与原子发布；推理委托官方 `IndexTTS2.infer()` |
| HTTP adapters | `modules/{indextts,gpt_sovits}/client.py` | 请求契约映射、错误分类与产物发布；底层复用 HTTPX |
| reference/manifest 绑定 | `models/schemas.py` | 结构上禁止 `prompt_text` 错配；复用 Pydantic 强类型 |

## 自研（custom）

| 模块 | 理由 |
|---|---|
| `atomic_output.py` | Windows `O_CREAT|O_EXCL` 保留 + `os.replace` 原子发布 + ownership/rollback；未发现维护活跃的现成组件 |
| `wav_probe.py` | 基于 soundfile+numpy 的固定阈值门禁；VAD/ASR 超出批次 1 范围 |
| `gpu_queue.py` | 单消费者 asyncio 队列；引入 SQLite/Redis/Celery 属于明确非目标 |
| `supervisor.py`/`process.py` | 双引擎生命周期、deadline 共享、进程树终止与 PID registry；POSIX supervisor 类库语义不符，psutil 已作为底层复用 |

所有自研模块均复用成熟底层库，不复制上游源码。

## 锁定一致性

- 引擎源码 pin 见 `config/engines.lock.yaml`（Index `90ca4d6…`，GSV `d523079…`）；
- 模型权重 pin 见 `config/engines.lock.yaml`（IndexTTS-2 `740dcaf…`，GSV pretrained `4fae8ec…`）；
- Python 依赖 pin 见 `pyproject.toml` + `uv.lock` 与 `config/env-locks/*`；
- checkpoint 资产哈希见 `config/checkpoints.lock.yaml`（由 `lock-engine-assets.ps1` 生成）。
