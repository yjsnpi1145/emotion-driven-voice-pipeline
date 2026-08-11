# ASR 文本评分开关设计

## 目标

在 WebUI 顶栏“关闭所有服务”按钮旁增加一个可持久化的“ASR 文本评分”开关。用户关闭后，新生成的参考音频不再因为转写文本与预期文本相似度不足而失败；重新开启后，后续新参考任务恢复当前严格评分。

## 边界

- 关闭的只是 ASR 文本相似度判定，不关闭 WAV 探测、3–10 秒参考窗口、静音/语音时长和 VAD 比例检查。
- Faster-Whisper 仍可执行转写，以便复用其 VAD 时间段并保留诊断 transcript；关闭状态把 `text`/`text_failed` 检查改记为 `text_skipped`。
- 开关只决定之后开始的参考质量分析，不取消或改变正在执行的任务。
- 已保存的参考版本按其生成时的质量设置保持可用；切换开关不会让当前参考音频失效，也不会自动重生成。

## 架构

新增 `RuntimeQualityGate`，包装现有 `DeterministicQualityAnalyzer` 或 `FasterWhisperQualityAnalyzer`，继续实现 `QualityAnalyzer` 协议。

- 开启时直接使用底层报告，策略指纹保持为底层指纹，因此现有参考版本和缓存兼容。
- 关闭时仍调用底层分析器，但忽略纯文本失败；VAD/时长失败仍失败。关闭状态使用由底层指纹和 `asr_text_scoring_enabled=false` 派生的独立指纹，防止严格报告与跳过文本评分的报告发生缓存碰撞。
- 保存报告校验同时接受当前底层指纹和同一底层策略派生的“文本跳过”指纹，从而保证切换不破坏已经生成的参考版本。
- 更新与分析通过异步锁串行化，保证已经进入分析的任务使用旧设置，更新完成后进入的任务使用新设置。

## 持久化与 API

设置保存在 `runtime/state/quality-settings.json`，采用原子临时文件替换：

```json
{"schema_version":1,"asr_text_scoring_enabled":true}
```

新增接口：

- `GET /api/v1/settings/quality`
- `PUT /api/v1/settings/quality`

视图返回 `schema_version`、`asr_text_scoring_enabled` 和 `source=config|runtime`。更新请求只接受 schema 与布尔值，未知字段返回 422。健康接口的 `quality` 节点增加 `asr_text_scoring_enabled`。

## WebUI

- 在“关闭所有服务”左侧放置 checkbox 风格开关，标签为“ASR 文本评分”。
- 页面初始化与全局刷新时读取服务端设置；不依赖 `localStorage`。
- 勾选表示开启；取消勾选后调用 PUT 并提示“ASR 文本评分已关闭；时长和 VAD 检查仍启用”。
- 保存期间禁用开关；失败时恢复服务端状态并显示错误。
- 服务不可用时禁用开关，关闭全部服务的现有行为不变。

## 质量报告语义

关闭状态：

- `checks` 中 `text` 或 `text_failed` 统一变为 `text_skipped`。
- 只有 `duration_failed`、`speech_failed` 或 `ratio_failed` 才产生 `QUALITY_VAD_FAILED`。
- VAD 通过时 `passed=true`、`failure_code=null`，即使相似度低于阈值。
- transcript、归一化文本、相似度和语言信息保留，用于版本历史诊断。

## 验收

1. 纯文本不匹配在开关开启时失败、关闭时通过。
2. VAD/时长失败在关闭状态下仍失败。
3. 开关持久化后重建应用仍保持关闭。
4. 严格模式生成的旧参考和关闭评分后生成的参考在切换后仍可用于 GSV。
5. API、健康响应与 WebUI 开关状态一致。
6. 全量 CPU 测试、Ruff、Mypy、JavaScript 语法和 GitHub CI 通过。
7. 本机真实服务关闭 ASR 文本评分后，用一个预期文本与转写明显不匹配但 VAD/时长合格的参考任务验证成功，再恢复用户选择的最终开关状态。
