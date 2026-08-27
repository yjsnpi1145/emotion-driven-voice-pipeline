# 隐私与本地数据边界

## 默认保存在本机的数据

- 参考音色、IndexTTS2 参考音频、GSV 分块音频和最终成品；
- 章节原文、翻译结果、分块、情绪向量和版本历史；
- SQLite 数据库、任务 manifest、运行日志和模型档案；
- 用户导入的 GPT-SoVITS 模型；
- LLM API Key 和运行时 LLM 设置。

这些内容位于 Git 忽略的 `runtime/`、`models/` 或 `external/` 目录。本项目不包含遥测、
统计上报、广告 SDK 或 CDN 依赖。

## 会发送到外部 LLM 的数据

当 `llm.mode` 为 `openai` 时，控制面会向用户配置的 OpenAI 兼容
`POST /chat/completions` 端点发送：

- 章节原文；
- 导演模式选择“配音改写”时的结构清洗段落；
- 目标语言；
- 分块 JSON schema 和规划指令；
- 参考文本修正时的当前中文参考文本、情绪描述和修正方向。

实际的数据处理和保留策略由用户选择的 API 服务商决定。fake 模式不会调用外部 LLM。

## API Key

API Key 由控制面保存到 `runtime/state/llm-secret.txt`，不写入 SQLite、日志、Git、WebUI
源码或 GET API 响应。浏览器只把用户输入的 Key 发送到本机 loopback 控制面；之后不会
回显明文。活动窗口不会记录 Authorization、请求头、Base URL、完整 prompt 或 Key。

## 声音与模型

使用者应只导入、训练、克隆和发布自己有权使用的声音。删除章节会移除工作台记录及其受管
产物，但已经复制、导出或备份到其他位置的文件不会自动删除。

## 公开问题报告

提交 Issue 前应删除 API Key、本机绝对路径、私人原文、声音文件、模型权重、数据库和未脱敏
日志。可使用 `/api/v1/health` 的状态字段描述问题，不要上传整个 `runtime/` 目录。

