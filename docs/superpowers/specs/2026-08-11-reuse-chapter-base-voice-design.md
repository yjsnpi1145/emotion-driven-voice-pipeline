# 局部重生成复用章节总参考音色设计

日期：2026-08-11
状态：设计通过，进入实施

## 1. 目标

在分块工作台执行“重新生成参考音频”或“重新生成两者”时，参考音色覆盖路径允许留空。
留空表示复用创建章节时选择的总参考音色；用户显式填写路径时，仅覆盖本次局部生成。

## 2. 数据流

1. WebUI 对局部参考音色使用可选输入框，默认留空；
2. API 的 `SegmentReferenceJobRequest.base_voice_path` 和
   `SegmentBothRegenerationRequest.base_voice_path` 改为可选；
3. `SegmentRegenerationService` 在提交参考任务前解析实际路径；
4. 若存在显式覆盖，直接使用覆盖路径；
5. 若留空，通过 `segment_id → chapter_run_segments → chapter_runs.snapshot_json` 读取章节
   创建请求中冻结的 `base_voice_path`；
6. 回退路径必须是绝对、存在、非符号链接的普通文件，并且内容 SHA-256 必须等于
   `chapter_runs.base_voice_sha256`；
7. 解析完成后仍生成普通的冻结 `ReferenceJobRequest`，执行器和 IndexTTS2 接口不变。

章节创建时应把已经解析为绝对路径的参考音色写入私有快照，避免未来章节保存相对路径。
已有章节的绝对路径快照继续兼容。

## 3. 错误处理

- 分块不属于章节：返回 `INVALID_INPUT`，要求显式选择参考音色；
- 章节私有快照缺少或损坏：返回 `DATABASE_INTEGRITY_FAILED`；
- 总参考音色已删除、变为符号链接或内容发生变化：返回 `INVALID_INPUT`，提示选择覆盖音色；
- 显式覆盖路径沿用现有参考任务输入校验。

## 4. 隐私边界

章节私有 `snapshot_json` 已保存原始创建请求，本次不新增公开字段。以下接口继续禁止返回
总参考音色路径：

- 章节列表和章节详情；
- 章节进度和 SSE；
- 分块历史。

## 5. WebUI

输入框文案改为“可选覆盖音色路径”，placeholder 明确“留空则复用章节总参考音色”。
前端仅在非空时发送 `base_voice_path`，删除“必须先填写”的拦截。

## 6. 验收

1. 局部重新生成参考音频时只发送 `request_id` 也能成功；
2. “重新生成两者”留空后先生成参考，再生成 GSV；
3. 显式覆盖路径仍优先；
4. 被修改或删除的总参考音色不会被静默使用；
5. 路径不出现在公共章节、进度或历史响应；
6. 完整非 GPU 测试和 Windows CI 通过。
