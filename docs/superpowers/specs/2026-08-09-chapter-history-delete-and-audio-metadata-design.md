# 章节历史删除与音频元数据加载设计

## 目标

1. 章节历史的每条记录提供独立“删除”按钮。
2. 删除操作从章节历史和工作台中隐藏该章节，但保留已生成分块、版本和音频文件，避免误删本地成果；磁盘清理由既有保留策略负责。
3. 正在排队或运行的章节不能删除，避免后台任务写入一个已隐藏的章节。
4. 分块当前参考、当前 GSV、版本历史和整篇成品的播放器在未播放前即可读取 WAV 元数据并显示真实时长，而不是 `0:00 / 0:00`。

## 删除语义

- 新增 `DELETE /api/v1/chapters/{run_id}`，成功返回 `200 {"status":"deleted","run_id":"..."}`。
- 删除为软删除：`chapter_runs.deleted_at_utc` 写入时间戳。
- `GET /api/v1/chapters`、章节详情、进度、事件、音频和时间线均不再返回已删除章节。
- 仅 `succeeded`、`failed`、`cancelled`、`interrupted` 允许删除；`queued`、`running` 返回 HTTP 409。
- 分块、任务、作业、不可变版本、缓存和内容寻址 WAV 不删除。这样不会破坏版本引用或共享缓存，也能防止误操作造成不可恢复的数据损失。
- UI 删除前显示本地确认框，明确说明“从章节历史移除，已生成音频版本仍保留”。删除当前选中章节后关闭 SSE，清空编辑区，并自动选择下一条历史记录。

## 音频根因与修复

现有所有 `<audio>` 使用 `preload="none"`，浏览器不会主动请求 WAV 头，因此原生控件只能显示 `0:00 / 0:00`。版本音频接口已经正确返回 `Content-Type: audio/wav`、`Accept-Ranges: bytes`，并支持 HTTP 206；实际截图中的参考版本时长为 9.102 秒，文件本身正常。

所有工作台播放器统一改为 `preload="metadata"`（JavaScript 动态播放器使用 `player.preload = "metadata"`）。浏览器只需读取 WAV 头和必要范围，不会预下载全部长音频。

## 数据库迁移

新增 Alembic revision `0003_chapter_history_soft_delete`，为 `chapter_runs` 添加 nullable 文本列 `deleted_at_utc`。打包头更新为该 revision。旧数据迁移后该列为 null，继续正常显示。

## 验收

- 终态章节删除后从列表消失，详情返回 404，底层任务和版本仍存在。
- 运行中章节删除返回 409且仍可查询。
- 删除当前选中章节时页面不会继续持有旧 SSE 或旧编辑状态。
- 所有生成的音频标签均使用 metadata preload，代码中不再存在 `preload="none"` 或 `player.preload = "none"`。
- 音频 GET 与 Range GET 继续分别返回 200 和 206。
