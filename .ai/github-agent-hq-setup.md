# GitHub Agent HQ 配置清单

本仓库已经提供 Issue 模板、PR 协作清单和 AI 产物校验 Workflow。GitHub 侧的 Agent HQ 开关、仓库权限、分支保护和订阅属于账户/组织设置，不能仅靠仓库文件自动开启。

## 首次配置

1. 确认该仓库已推送到 GitHub，并确认 Claude Code / Codex 对仓库的授权范围。
2. 在 GitHub 的 Agent HQ 中选择一个 Agent 做 Architect，另一个做 Implementer；不要让两个 Agent 同时写同一工作区。
3. 新建 Issue 时使用 `AI task` 模板，并把任务文件同步到 `.ai/tasks/`。
4. 实现 Agent 只能提交 Draft PR；审查 Agent 通过 PR 评论或 Review 给出 `PASS` / `REQUEST_CHANGES` / `BLOCKED`。
5. 在仓库分支保护中将 `AI collaboration contract / Validate AI handoff artifacts` 设为必需检查（Workflow 首次运行后才会出现在可选检查中）。
6. `main` 分支保留人工批准和合并权限，不给 Agent force-push、删除分支或修改保护规则的权限。

## Issue / PR 约定

- 一个 Issue 对应一个 `.ai/tasks/task-*.md`。
- 一个 Draft PR 对应一个实现分支和一个 `.ai/handoffs/task-*-handoff.md`。
- 审查结果必须落到 `.ai/reviews/task-*-review.md`，并在 PR 描述中链接该文件。
- `PASS` 不是人工批准；高风险变更仍需人类确认。

## 本机工作区

当前机器未检测到 `gh`（GitHub CLI），且该仓库当前没有配置 Git remote。因此本次只完成了可提交的仓库协作层；推送仓库、授权 Agent、配置 Agent HQ 和保护 `main` 需要你在 GitHub 账户侧完成。
