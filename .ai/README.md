# 本地 + GitHub Agent HQ 协作入口

## 推荐流程

1. 在 GitHub 创建 AI Task Issue，使用 `.github/ISSUE_TEMPLATE/ai-task.md`。
2. 在 Agent HQ 中先让 Claude Code 做架构分析或任务拆解。
3. 让 Codex 在独立分支 / worktree 实现，并提交 Draft PR。
4. 在 Draft PR 中请求 Claude Code 独立审查。
5. 将审查意见写入 `.ai/reviews/`，再让 Codex 逐条修复。
6. 运行 CI 和本地验证，确认状态为 `VERIFIED`。
7. 人类最终批准合并。

Issue / PR 评论应引用任务文件和审查文件，不要把完整凭据或本机敏感路径粘贴到 GitHub。

## 产物分层

- `.ai/tasks/`：任务契约，提交到仓库。
- `.ai/reviews/`：独立审查记录，提交到仓库。
- `.ai/handoffs/`：实现交接记录，提交到仓库。
- `.ai/runs/`：临时计划、日志和 Agent 运行输出，默认被 `.gitignore` 忽略；不要把计划误放到 `.ai/handoffs/`。

## 本地脚本

先创建任务文件：

```powershell
Copy-Item .ai/templates/task.md .ai/tasks/task-001.md
```

仅运行 Claude 规划：

```powershell
.\scripts\ai\run-agent.ps1 -Agent claude -PromptFile .ai/tasks/task-001.md -OutputFile .ai/runs/task-001-plan.md -ReadOnly
```

让 Codex 实现（明确允许写入）：

```powershell
.\scripts\ai\run-agent.ps1 -Agent codex -PromptFile .ai/tasks/task-001.md -OutputFile .ai/handoffs/task-001-handoff.md -AllowWrite
```

让 Claude 只读审查当前 diff：

```powershell
.\scripts\ai\run-agent.ps1 -Agent claude -PromptFile .ai/tasks/task-001.md -OutputFile .ai/reviews/task-001-review.md -Review -ReadOnly
```

验证 `.ai` 产物：

```powershell
.\scripts\ai\validate-contract.ps1
```

完整接力流程（会实际调用 Agent，必须显式允许写入）：

```powershell
.\scripts\ai\review-loop.ps1 -TaskFile .ai/tasks/task-001.md -AllowWrite
```

## 当前仓库注意事项

本仓库当前已有未提交业务和临时文件。脚本默认不执行破坏性 Git 操作，也不会自动 stash 或清理工作区。建议先在独立 worktree 中使用。

