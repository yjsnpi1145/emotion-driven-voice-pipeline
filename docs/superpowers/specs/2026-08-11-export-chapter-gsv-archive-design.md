# 章节 GSV 分块打包导出设计

## 目标

在配音工作台的当前章节工具栏增加“导出全部分块 GSV”按钮。用户点击后，下载一个 ZIP；其中每个分块只导出当前激活的 GSV 版本，并按章节顺序命名，同时附带不含本地路径的 JSON 清单。

## 方案选择

### 采用：服务端按需生成临时 ZIP

后端读取章节的持久化分块顺序和每段当前 GSV 指针，验证不可变音频后在受管 artifact 根目录创建临时 ZIP。`FileResponse` 完成传输后删除临时文件。

优点：浏览器不需要把所有 WAV 读进内存；ZIP 内容由后端的版本指针和哈希校验统一保证；不新增第三方依赖。缺点：每次下载都需要重新压缩。

### 未采用：持久化导出包

为每次导出保留 ZIP 可以加快重复下载，但会引入缓存键、保留策略和过期清理，超出当前需求。

### 未采用：浏览器端打包

浏览器逐个下载 WAV 再打包会占用大量内存，还需要引入 ZIP 前端依赖，并且难以在一个一致快照内冻结所有当前版本。

## 后端契约

新增：

```text
GET /api/v1/chapters/{run_id}/export/gsv
```

成功返回 `application/zip`，文件名为安全化章节标题加 `-gsv-segments.zip`。ZIP 内容：

```text
001.wav
002.wav
...
manifest.json
```

序号宽度至少三位，并随章节分块总数扩展。`manifest.json` 使用 UTF-8，包含：

- `schema_version: 1`
- `run_id`
- `title`
- `created_at_utc`
- 有序 `segments` 数组；每项包含 `ordinal`、`segment_id`、`version_id`、`file_name`、`content_sha256`、`synthesis_text`、`target_language` 和 `ref_version_id`

清单不得包含 artifact 根目录、模型路径、基础音色路径或其他本地绝对路径。

## 一致性与安全

1. 导出开始时读取章节当前分块列表；每个分块必须存在 `active_gsv_version_id`。
2. 当前版本必须属于对应分块、类型为 `gsv`、状态为 `ready`。
3. 数据库 blob 相对路径必须与 content-addressed 规范路径一致。
4. WAV 必须是 artifact `blobs` 根内的普通非符号链接文件，且实时 SHA-256 必须等于版本记录。
5. 任一分块缺失当前 GSV 时，整次请求返回 `409 CHAPTER_STATE_CONFLICT`，`details.missing_ordinals` 列出缺失序号，不生成不完整包。
6. 任一 blob 缺失或损坏时，整次请求返回 `409 ARTIFACT_MISSING` 或 `409 ARTIFACT_CORRUPT`。
7. ZIP 在工作线程中创建，避免阻塞异步事件循环；失败和传输完成都清理临时文件。

导出冻结的是点击时的当前版本集合。导出期间发生的新版本激活不会改变已经构建的 ZIP。

## WebUI

在“重新拼接整篇”旁增加次要按钮“导出全部分块 GSV”。

- 未选择章节或任一分块没有当前 GSV 时禁用。
- 生成中但已有旧的当前 GSV 时仍允许导出，导出的是点击时仍处于激活状态的旧版本。
- 按钮使用普通下载链接，浏览器直接流式保存 ZIP，不先把整个文件读入 JavaScript 内存。
- 提示文字明确说明导出的是“每段当前 GSV 版本”。

## 测试与验收

1. 两分块章节生成完成后，导出响应为 ZIP，包含 `001.wav`、`002.wav` 和 `manifest.json`。
2. 两个 WAV 均为 RIFF 文件；清单顺序、版本 ID 和 SHA-256 与 progress 当前指针一致。
3. 响应和清单不泄露本地基础音色及 artifact 路径。
4. 任一分块缺少当前 GSV 时返回 409，并准确列出缺失序号。
5. 修改一个受管 blob 后导出返回损坏错误，不输出部分 ZIP。
6. WebUI 契约验证按钮、下载 URL、禁用逻辑和提示文案。
7. 运行完整非 GPU 测试、Ruff、mypy、Node 语法检查，并在真实本地服务下载、解包验证。
