# 导演模式角色复核恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让被历史版本错误锁在翻译校对阶段的待确认语句能够直接恢复角色编辑。

**Architecture:** 在现有 `director-dnd.js` 中增加纯状态门控函数，前端统一使用它解锁两种复核阶段的角色结构控件。后端现有角色变更路径负责把项目回退到 `role_review`，并通过 API 回归测试固定这一行为。

**Tech Stack:** 原生 ES modules、FastAPI、SQLAlchemy、pytest/httpx、Node.js。

## Global Constraints

- 只解锁角色结构操作，不解锁翻译校对阶段的配音文本编辑。
- 翻译校对阶段发生结构修改后必须回到 `role_review`。
- 不直接修改用户数据库，不增加破坏性迁移。

---

### Task 1: 状态门控与后端回退测试

**Files:**
- Modify: `tests/unit/test_director_dnd_js.py`
- Modify: `tests/contract/test_director_api.py`
- Modify: `src/voice_pipeline/webui/director-dnd.js`

- [ ] 写失败测试：`canEditRoleReview()` 对 `role_review`、`translation_review` 返回 true，其余阶段返回 false。
- [ ] 写 API 测试：翻译校对阶段修改 `role_confirmed` 后项目状态回到 `role_review`。
- [ ] 运行目标测试确认前端辅助函数测试失败。
- [ ] 实现 `canEditRoleReview(status)` 并运行目标测试通过。
- [ ] 提交：`test: cover director review recovery`。

### Task 2: 前端解锁恢复操作

**Files:**
- Modify: `src/voice_pipeline/webui/director.js`
- Modify: `tests/contract/test_workbench_api.py`

- [ ] 写失败合同测试，要求角色卡片、角色下拉、配音、确认、拆分和合并统一使用 `canEditRoleReview()`。
- [ ] 导入辅助函数并替换仅检查 `status === "role_review"` 的角色结构门控。
- [ ] 为翻译校对阶段控件增加“修改后返回角色复核并重新翻译”提示。
- [ ] 运行目标测试及 `node --check`。
- [ ] 提交：`fix: allow director review recovery`。

### Task 3: 验证与部署

**Files:**
- Verify only.

- [ ] 运行 Ruff、Mypy、JavaScript 检查及全量非 GPU 测试。
- [ ] 构建 wheel 并检查更新后的 WebUI 资源。
- [ ] 推送 PR、通过 CI、合并并安装合并版本。
- [ ] 在当前异常项目中确认黄色语句的“确认角色”、角色选择和拆分控件已经可用；不代替用户提交实际内容修改。
- [ ] 确认服务 ready、队列空闲、工作树干净、`HEAD == origin/main`。
