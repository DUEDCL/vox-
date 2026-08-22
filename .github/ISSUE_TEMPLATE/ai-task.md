name: AI task
about: 创建一个可由 Claude Code / Codex 接力执行的任务
labels: ["ai-task"]

---

## 目标

<!-- 用用户可验证的结果描述目标。 -->

## 验收标准

- [ ]
- [ ]
- [ ] 异常和边界行为已覆盖

## Agent 分工

- 架构 / 审查：Claude Code
- 实现 / 修复：Codex
- 最终合并：人类

## 允许修改

- 

## 禁止修改

- `enrollment/`、`.env`、模型权重和凭据
- `contracts/voice-events.schema.json`
- 未经批准的核心依赖、数据库或部署配置

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

## 风险和人工闸门

- [ ] 不涉及身份、隐私、音频数据、权限或凭据
- [ ] 不涉及数据库结构或核心依赖
- [ ] 不涉及部署和 CI 权限

## 交接文件

- 任务：`.ai/tasks/task-xxx.md`
- 审查：`.ai/reviews/task-xxx-review.md`
- 交接：`.ai/handoffs/task-xxx-handoff.md`
