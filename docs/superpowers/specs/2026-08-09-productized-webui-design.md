# 产品化本地 WebUI 设计

日期：2026-08-09
状态：设计通过，按既有“文档无需逐次批准、连续执行”约定实施

## 1. 目标

在保留现有 FastAPI、SQLite、单 GPU 队列和不可变音频版本契约的前提下，把当前单页工具打磨为适合长期本地配音工作的桌面控制台。界面借鉴 GPT-SoVITS 整合包 WebUI 的页签、分组面板、主按钮和紧凑参数布局，但不嵌入 Gradio，也不复制上游实现。

用户应能在同一个 WebUI 内完成：

1. 创建章节、浏览分块、微调草稿、局部重生成和整篇拼接；
2. 管理并切换 GPT-SoVITS 模型档案；
3. 用原生文件选择器选择 `.ckpt`、`.pth` 和参考 WAV；
4. 在 Windows 资源管理器中打开模型库、具体模型、产物和日志目录；
5. 查看、测试、修改并立即应用 OpenAI 兼容 LLM 设置；
6. 查看服务、引擎、存储、队列和任务状态；
7. 在窄屏与常规桌面分辨率下保持可用。

## 2. 方案选择

### 采用：原生 FastAPI SPA + 本地桌面桥接

- 保留现有 `/api/v1` 控制面和零 Node 构建依赖；
- 用语义化 HTML、模块化原生 JavaScript 和 CSS 实现 GPT-SoVITS 风格页签；
- 新增严格枚举化的本地桌面操作 API，不接受任意 shell 命令；
- 新增可热更新的 LLM Director 管理器，新章节读取保存后的最新设置；
- 设置和密钥保存到 Git 忽略的 `runtime/state`，API 永不回传密钥。

### 不采用：嵌入原 GPT-SoVITS Gradio WebUI

会产生第二套状态、端口和模型选择逻辑，无法与本项目的任务、版本、缓存和单 GPU 队列一致。

### 不采用：现在迁移 React/Vue

会引入新的构建链和依赖锁，当前产品化需求不需要组件框架才能实现。

## 3. 信息架构

顶部保留品牌、服务状态、当前模型和快捷操作，主导航分四页：

- **配音工作台**：章节创建、章节列表、分块列表、分块编辑器；
- **模型管理**：活动模型、模型库路径、导入向导、模型档案卡片；
- **LLM 设置**：模式、OpenAI Base URL、模型、API Key、超时、重试和参考文本修正次数；
- **系统状态**：控制面、两引擎、存储、任务队列、GPU 队列，以及日志/产物目录快捷入口。

全局反馈使用右下角 toast；长操作按钮显示 busy；错误信息经过路径脱敏。页签切换不销毁工作台草稿，后台 SSE 继续运行。

## 4. 本地桌面桥接

新增以下 loopback-only API：

```text
GET  /api/v1/local/paths
POST /api/v1/local/open-folder
POST /api/v1/local/pick-file
POST /api/v1/model-profiles/{profile_id}/open-folder
```

`open-folder` 只接受 `model_library | model_sources | artifacts | logs` 四个资源键。服务端把键映射到配置内路径并再次验证为允许目录；不得接受原始命令或任意路径。

`pick-file` 只接受 `gpt_weight | sovits_weight | base_voice`，分别限制为 `.ckpt`、`.pth` 和 `.wav`。实现复用 Windows 原生 `tkinter.filedialog`；取消选择返回 `selected=false`，不视为错误。每个具体模型目录由 profile ID 在模型库内部解析。

## 5. LLM 设置

运行时设置模型：

```text
mode: fake | openai
base_url: http(s) URL
model: non-blank string
timeout_seconds: 1..300
max_retries: 0..5
max_reference_corrections: 0..5
api_key: write-only optional secret
```

新增：

```text
GET  /api/v1/settings/llm
PUT  /api/v1/settings/llm
POST /api/v1/settings/llm/test
```

保存使用临时文件、`fsync` 和 `os.replace`。非密钥设置保存为 `runtime/state/llm-settings.json`；密钥保存为 `runtime/state/llm-secret.txt`，权限尽量收紧为当前用户。响应只包含 `api_key_configured: bool`。

`RuntimeDirector` 实现现有 Director 协议，并在异步锁内代理当前 `FakeDirector` 或 `OpenAiDirectorClient`。更新期间不打断已开始的 LLM 调用；更新完成后新章节立即使用新客户端。ChapterService 从 RuntimeDirector 动态读取 `max_reference_corrections`，不再冻结启动配置值。

“测试连接”使用表单中的候选配置创建临时客户端，发送要求返回 `{\"ok\": true}` 的最小 OpenAI Chat Completions 请求；成功返回延迟，失败返回结构化 LLM 错误，且不会保存候选设置。

## 6. 模型管理体验

- 模型管理页顶部显示活动档案和模型库位置；
- “打开模型库”直接打开 Windows 资源管理器；
- GPT/SoVITS 路径输入旁提供“浏览”按钮；
- 每个档案卡显示名称、状态、模型家族、短哈希、导入时间、激活按钮和“打开文件夹”；
- 导入后刷新列表但不自动激活，延续现有安全契约；
- 工作台模型下拉和顶部当前模型徽标同步更新。

## 7. 系统状态体验

系统页每次打开和手动刷新时读取 `/api/v1/health`。引擎 `stopped_expected` 在独占进程模式下显示为“按需启动”，不显示为故障。路径卡只显示本地目录；API Key、完整请求快照和私有诊断不进入 DOM。

## 8. 错误与并发

- LLM 保存/测试、文件选择和打开目录各自有独立 busy 锁，防止重复提交；
- 保存设置期间不影响已开始的章节编排；
- 文件选择取消不清空已有输入；
- 打开不存在目录返回 409；未知资源键由 Pydantic 返回 422；
- RuntimeDirector 客户端切换失败时保留上一份可用设置；
- WebUI 沿用 run/segment generation token、dirty 草稿保护和 SSE 迟到结果保护。

## 9. 验收

1. 根页面包含四个产品页签，默认进入配音工作台；
2. 打开模型库和具体档案只会访问允许目录；
3. 文件选择类型与扩展名严格匹配，取消无副作用；
4. LLM GET 不回传密钥，保存后重启仍恢复设置和密钥状态；
5. 测试连接不保存候选值；
6. 更新 LLM 后新章节使用新客户端，运行中调用不被关闭；
7. 模型切换、导入、局部重生成、版本历史和整篇拼接原有契约不回归；
8. 系统页能区分 ready、按需启动、错误和 busy；
9. 1440×900、1280×720 和窄屏布局无水平溢出；
10. 无外部 CDN、无 API Key DOM 回显、无任意路径/命令执行入口。

