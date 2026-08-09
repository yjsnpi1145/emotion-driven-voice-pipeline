# 章节任务阶段进度条设计

## 目标

在配音工作台左侧“章节历史”上方展示当前任务的阶段进度，让用户能立即判断任务处于文本规划、参考音频、GSV 合成还是整篇拼接阶段，并看到分块完成数量。

## 方案选择

评估过三种方案：

1. 仅按 `queued/running/succeeded` 显示一个百分比：实现简单，但无法解释 GPU 当前在做什么。
2. **由现有章节与分块进度派生四段式进度（采用）**：无需新增持久化状态，且能显示每类音频的真实完成数量。
3. 在数据库中新增显式 orchestration stage：最精确，但会重复记录已经可由 job/版本指针推导的信息，并增加恢复与迁移成本。

## 交互与视觉

进度组件位于新建章节折叠区之后、章节历史标题之前。包含：

- 总体状态和总体百分比；
- 四段进度轨道；
- 四个阶段标签：`文本规划`、`参考音频`、`GSV 合成`、`整篇拼接`；
- 参考与 GSV 阶段显示 `完成数/总分块数`；
- 当前阶段使用蓝色高亮和轻量动画，完成为绿色，失败/中断为红色，未开始为灰色；
- 无章节时显示空闲状态，不伪造百分比；
- 提交新章节且服务端仍在调用 LLM 时，优先显示“文本规划中”；
- 响应式布局在窄侧栏中保持两列阶段标签，在手机宽度下降为单列。

组件使用 `role="progressbar"`、`aria-valuenow`、`aria-valuemin`、`aria-valuemax` 和可读阶段文本。

## 进度派生规则

进度仅使用公开数据：当前 `run` 与 `/api/v1/chapters/{run_id}/progress`。

- 文本规划：服务端成功返回章节 run 后为 100%；POST 等待期间为 active/indeterminate。
- 参考音频：具有 `active_ref_version_id` 的分块数除以总分块数。
- GSV 合成：具有 `active_gsv_version_id` 的分块数除以总分块数。
- 整篇拼接：`final_audio_url` 存在时为 100%。
- 总体百分比：四个阶段完成比例的算术平均值。
- 当前阶段优先取 queued/running 的 reference/GSV job；没有活动 job 时取第一个未完成阶段。
- `succeeded` 强制四段完成；`failed/cancelled/interrupted` 将当前未完成阶段标为停止状态，不清除此前进度。
- 后续草稿编辑或局部重生成不会让已经完成的章节任务进度倒退；成功章节保持 100%。

## 代码边界

- `src/voice_pipeline/webui/stage-progress.js`：纯函数，定义阶段与派生规则，不访问 DOM。
- `src/voice_pipeline/webui/app.js`：维护提交中的本地 planning 状态并渲染组件。
- `src/voice_pipeline/webui/index.html`：提供进度组件挂载点。
- `src/voice_pipeline/webui/styles.css`：四段轨道、阶段状态与响应式样式。
- `src/voice_pipeline/api/workbench_routes.py`：将纯函数模块加入静态文件白名单。

不修改数据库、章节 API schema 或 GPU 调度流程。

## 错误处理

- LLM/提交失败：保留规划阶段失败状态并显示现有错误 toast；再次提交会重置。
- 章节执行失败/中断：进度停在第一项未完成阶段；已完成比例保持。
- progress 暂时不可用：沿用当前页面请求错误处理，不显示猜测数据。

## 验收

1. 无任务时显示“尚未选择任务”。
2. 提交期间显示“文本规划中”。
3. 运行任务按 reference/GSV 版本数量显示分块进度。
4. 所有 GSV 完成但整篇未发布时高亮“整篇拼接”。
5. 成功任务显示 100% 和四段完成。
6. 失败/中断任务显示停止阶段。
7. SSE/轮询刷新能实时更新进度，切换章节不会串数据。
8. 侧栏和移动端布局不溢出。
