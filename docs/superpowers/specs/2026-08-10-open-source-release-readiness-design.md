# 开源发布准备设计

## 目标

把当前仅在开发机器上可运行的项目整理为可公开托管、可在新的 Windows 路径中安装、可通过 GitHub CI 验证的源码仓库，同时确保模型、声音、密钥、数据库和本机配置不会进入 Git。

## 发布形态

- 仓库只发布本项目源码、WebUI、脚本、配置模板、测试和文档。
- 不发布 IndexTTS2/GPT-SoVITS/ASR 权重、用户训练模型、参考音色、生成音频、SQLite 数据库或 LLM 密钥。
- 首个版本为 `v0.1.0` 源码发布；wheel 可以作为 Release 附件，但不制作包含模型的整合包。
- 支持平台明确限定为 Windows 10/11、PowerShell 7、Python 3.11 和 NVIDIA CUDA；fake 模式不要求 GPU 或模型。

## 许可证边界

- 本项目原创控制面、CLI、WebUI、worker 外壳和测试使用 Apache License 2.0。
- 根目录提供 `LICENSE`，`pyproject.toml` 声明 `Apache-2.0`。
- `THIRD_PARTY_NOTICES.md` 列出直接依赖和固定上游。
- `MODEL_LICENSES.md` 明确模型不随仓库分发，并分别链接固定版本许可证。
- IndexTTS2 固定提交采用 bilibili Model Use License Agreement，不能继续标为 MIT。
- GPT-SoVITS 固定提交的代码许可证为 MIT；预训练资产和用户训练权重仍按其来源、数据和模型条款单独判断。

## 可移植安装

- 所有 PowerShell 脚本通过 `$PSScriptRoot` 推导仓库根目录，不出现 `D:\TTSsystem` 默认值。
- README 命令从当前克隆目录运行，并指导用户从 `config/app.example.yaml` 创建不跟踪的 `.local.yaml`。
- setup 脚本继续固定上游 commit/revision 和资产哈希。
- 真实模型下载前显示模型许可证入口，并要求调用者显式传入接受参数；fake 模式不触发下载。

## 仓库卫生与隐私

- `.gitignore` 排除 `.env`、私钥、数据库、音频、模型权重、IDE 文件和本地运行产物，同时允许安全的示例文件。
- 增加自动仓库卫生测试，拒绝硬编码开发机路径、跟踪的敏感扩展名、缺失治理文件和错误的 IndexTTS2 许可证标签。
- `PRIVACY.md` 说明章节文本会发送到用户配置的 OpenAI 兼容端点；浏览器不直接持有 API Key；音频、模型、数据库和密钥默认仅保存在本机。
- `SECURITY.md` 要求安全报告不要附带真实密钥、私人声音或未经脱敏的运行目录。

## 公共文档

- README 改为当前完整产品说明，不再使用 “Batches 1–2” 或已失效的非目标描述。
- README 包含截图位置、架构、功能、系统要求、fake 快速启动、真实安装、LLM 配置、自训练 GSV 模型、测试、数据边界和许可证摘要。
- 详细 Windows 安装与故障排查放入 `docs/installation-windows.md`。
- 提供 `CONTRIBUTING.md`、`CHANGELOG.md`、Issue 表单和 PR 模板。

## 自动化

- `.github/workflows/ci.yml` 在 Windows + Python 3.11 上运行 uv lock 校验、Ruff、Mypy、JavaScript 语法检查、非 GPU 测试和 wheel 构建。
- CI 不下载真实模型，不使用任何仓库密钥，不执行 GPU/质量模型测试。
- GPU 黄金验收保留为本地或未来 self-hosted runner 流程。

## 验收

1. 仓库工作树干净，当前历史不含模型/音频/数据库或明显密钥。
2. 自动仓库卫生测试通过，所有公开 setup 脚本不包含开发机绝对路径。
3. Ruff、Mypy、JavaScript 语法检查和全部非 GPU 测试通过。
4. wheel 构建成功并包含 WebUI、许可证和声明文件。
5. 从临时目录克隆/复制 tracked tree 后，`uv sync --frozen --extra dev`、fake 服务启动和 `/api/v1/health` 验证成功。
6. README 的快速开始只使用仓库内已跟踪的文件。

