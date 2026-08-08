# GPT-SoVITS 模型档案与批次 2 设计

**状态：已确认（2026-08-08）**

## 背景与交接

批次 1 的自动化、三进程黑盒和真实引擎工程验证已经完成。人工黄金试听不是失败：用户明确豁免该门禁。因此交接状态为 `engineering_verified / golden_listening_waived_by_user`，不得记录成黄金试听已通过。

用户将在本机使用训练过的 GPT-SoVITS 权重。系统不能只以 GPT-SoVITS 基模推理，必须能够把用户指定的一条 GPT `.ckpt` 和一条 SoVITS `.pth` 导入本地模型库、显式切换，并把准确的模型身份冻结到作业、版本和缓存。

本设计把这些基础能力加入批次 2；不会提前实现 WebUI 或批次 5 的局部重生成工作流。

## 目标与非目标

### 目标

1. 将指定的单个 GPT/SoVITS 权重对原子导入项目管理的本地目录。
2. 将每一对权重作为不可变、可哈希、可选择的模型档案（model profile）。
3. 保留一个显式的 `base` 档案，但它只是一项可选档案，绝不作为切换或推理失败时的静默回退。
4. 使用 GPT-SoVITS 官方 `/set_gpt_weights`、`/set_sovits_weights` 和 `/tts` 接口，在单个 GPU 队列租约内串行热切换并推理。
5. 将选择的档案 ID、双文件 SHA-256、路径快照和引擎指纹写入持久化作业、音频版本、运行清单和缓存键。
6. 保持批次 1 的公共 API/CLI 兼容；未提供 `model_profile_id` 时，在创建作业的事务内冻结当前活动档案。

### 非目标

- 不上传权重文件；导入请求只引用本机绝对路径。
- 不扫描目录、猜测 GPT/SoVITS 配对或自动挑选“最新”权重。
- 不在批次 2 建模型选择 WebUI、导入向导、SSE 或试听界面（批次 4）。
- 不在批次 2 做模型切换后影响分析、批量重生成或版本激活 UX（批次 5）。
- 不复制、修改或重写 GPT-SoVITS 推理实现；只使用其公开 HTTP 切换端点。
- 不自动物理删除任何模型档案；批次 2 仅支持归档并保留依赖关系。

## 文件布局与不可变性

项目模型库根目录固定为：

```text
D:\TTSsystem\models\gpt-sovits\
  profiles\
    <profile-id>\
      profile.json
      GPT\model.ckpt
      SoVITS\model.pth
```

`profile-id` 是不可变 UUID。显示名是用户可读字段而非路径。导入器先把两个文件复制到同盘暂存目录 `models/gpt-sovits/.staging/<import-id>/`，流式计算 SHA-256，写入 `profile.json`，随后以目录重命名发布到 `profiles/<profile-id>`。数据库行在目录发布之后、同一个 SQLite 写事务内登记；发布前失败只清理暂存，不创建档案。

`profile.json` 至少包含：schema 版本、profile ID、显示名、来源文件绝对路径（审计用途）、导入时间、可选 `declared_family`、GPT/SoVITS 相对路径、长度和 SHA-256。权重路径始终相对模型库根保存。创建后禁止原地替换；用户训练出新版本时导入新的档案。模型家族仅供展示，不由本项目解析权重格式；官方加载端点是兼容性的最终裁决。

安装时建立一个显式 `base` 档案，权重也位于上述模型库。它可以被选中，但不是隐式默认回退。首次未导入训练档案的安装可将它设为活动档案；导入训练档案并激活后，新作业默认使用训练档案。

## 持久化模型

批次 2 的 SQLite schema 新增：

```text
model_profiles(
  profile_id PK, display_name, source_kind, declared_family nullable,
  relative_directory UNIQUE, gpt_relative_path, sovits_relative_path,
  gpt_sha256, sovits_sha256, gpt_size_bytes, sovits_size_bytes,
  status, created_at, archived_at nullable
)

project_settings(key PK, value)
```

`status` 枚举为 `ready | missing | corrupt | archived`。`project_settings.active_gsv_model_profile_id` 指向可用档案。物理模型文件、档案行或活动指针的任何变更都必须在事务边界上可恢复；启动恢复程序检测已发布目录而未登记的导入和登记后损坏/缺失的档案，并生成诊断而不假造可用状态。

GSV 作业快照、参考/目标 artifact、run manifest 和版本化缓存要保存：`model_profile_id`、GPT/SoVITS SHA-256、两个相对路径和 GPT-SoVITS engine fingerprint。缓存键必须包含这四个模型标识和既有的参考音频 SHA、文本、语言、推理参数、权重/引擎指纹；模型不同绝不能命中同一缓存。

普通音频版本的最近五版清理规则只处理 artifact；模型档案不参与该清理。任何已经引用档案的历史作业和版本在诊断中保留可追踪身份，即使模型后来被归档或损坏。

## 控制接口和 CLI

所有接口位于现有 loopback-only `/api/v1` 命名空间：

```text
POST /api/v1/model-profiles/import
GET  /api/v1/model-profiles
GET  /api/v1/model-profiles/{profile_id}
POST /api/v1/model-profiles/{profile_id}/activate
```

导入 body 包含非空 `display_name`、绝对 `gpt_source_path`、绝对 `sovits_source_path`，以及可选 `declared_family`。服务器验证 `.ckpt`/`.pth` 后缀、普通文件、非空文件和 configured import roots；导入完成返回新档案。活动切换只能指向 `ready` 且哈希仍匹配的档案；不重写任何已经创建作业的快照。

CLI 只能经 HTTP 访问这些端点：

```powershell
voice-pipeline model import --name "我的训练声线-v1" --gpt "D:\source\model.ckpt" --sovits "D:\source\model.pth"
voice-pipeline model list
voice-pipeline model activate <profile-id>
```

现有 GSV 请求添加可选 `model_profile_id`。缺省时，创建作业事务读取活动档案并写入该作业的不可变快照；不得等到 worker 真正运行时再读取活动设置。

## GPU 热切换协议

为每个 GSV 作业取得现有全局单消费者 GPU 队列租约后，适配器只按如下顺序工作：

```text
验证冻结的模型文件和 SHA
  -> GET /set_gpt_weights?weights_path=<imported absolute path>
  -> GET /set_sovits_weights?weights_path=<imported absolute path>
  -> POST /tts
  -> 原子发布输出与版本提交
```

每个 worker 进程代次都认为自己未装载任何已验证档案；即使上一任务使用同一档案，也完整设置两条权重。这一小额加载成本换取确定性。控制面维护的“已装载档案”只能作诊断，绝不可绕过切换调用。

任一切换请求超时、网络断开、返回非成功、或 `/tts` 结果不确定时，运行时必须把 GSV 进程视为不可信：停止/重启受管 worker、等待活动推理归零或进程树退出、阻止队列继续消费，直到恢复为可确认状态。该任务失败，且绝不调用 `/tts`、绝不自动尝试 `base`。这与批次 1 的超时、队列 poison 和进程生命周期契约一致。

## 质量、错误和迁移

导入成功仅表示文件副本与哈希有效；官方端点成功加载一对权重才表示运行时可用。`activate` 不做隐藏的语音生成。生成前文件缺失或哈希不符将把档案标为 `missing`/`corrupt` 并以结构化错误拒绝任务。路径、后缀、名称、源文件或磁盘写入错误必须在作业目录和数据库副作用前失败。

批次 2 的启动恢复语义仍是：`running` 作业转 `interrupted`，迟到提交受 OCC token 拒绝激活但保留诊断历史；持久 `queued` 作业以 lease 重投。模型切换由同一 execution snapshot 包含，不能破坏这些规则。

## 验收

1. 单元/集成：显式导入一对临时权重、哈希和目录不变性、重复/非法/中断导入、活动档案原子切换、活动档案变更不影响已入队任务、缓存隔离、归档与历史引用。
2. 黑盒：三个进程、独立解释器、可观测 fake GSV 服务。验证每次合成严格先后调用两个官方切换 URL 和 `/tts`；一个切换失败时 `/tts` 次数为零、`base` 次数为零、作业终态为失败且 runtime 执行恢复。
3. 恢复：在复制、目录发布、SQLite 发布、清理候选各故障点强制终止并重启，验证不出现可见半档案、错误 current 指针或误删当前 artifact。
4. 真实工程：以用户给出的训练权重对完成一次导入、选择和 HTTP 合成，核对 manifest 的 profile/hash 与模型库文件。此项不替代、也不改变已经豁免的批次 1 人工黄金试听。

## 后续批次边界

- **批次 3**：OpenAI 兼容 LLM、文本分块、批量编排和 final.wav；它只消费已经冻结的模型档案身份。
- **批次 4**：模型档案 WebUI 列表、状态、导入表单和活动档案下拉选择。
- **批次 5**：模型改变后的影响呈现、显式局部重生成、历史试听/激活/恢复与重拼。
