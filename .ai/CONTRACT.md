# AI 协作协议

## 目的

本协议把 GitHub Agent HQ 的 Issue / Pull Request 交接和本地 Claude Code + Codex Forge 风格流程统一起来。GitHub 上的 Issue、Draft PR、评论和 CI 是共享信箱；本地 `.ai/` 文件是可审计的任务状态和交接记录。

## 状态机

```text
TODO → IN_PROGRESS → REVIEW → CHANGES_REQUESTED → VERIFIED → MERGED
                         └──────────────→ BLOCKED
```

- `TODO`：只有目标和验收标准，尚未实现。
- `IN_PROGRESS`：实现者正在修改。
- `REVIEW`：实现者已提交交接，等待独立审查。
- `CHANGES_REQUESTED`：审查发现必须修复的问题。
- `VERIFIED`：审查通过且验证命令有证据。
- `BLOCKED`：依赖、权限、环境或需求冲突导致不能安全继续。
- `MERGED`：人类批准并完成合并。

## 角色边界

| 角色 | 默认 Agent | 允许做什么 | 不允许做什么 |
|---|---|---|---|
| Architect | Claude Code | 读代码、拆任务、写任务说明、识别风险 | 未授权大规模改代码 |
| Implementer | Codex | 修改允许范围、补测试、执行验证 | 修改禁止范围、批准自己 |
| Reviewer | Claude Code | 读 diff、运行只读检查、写审查报告 | 直接悄悄修复被审查代码 |
| Fixer | Codex | 只修复审查报告明确的问题 | 借机重构无关模块 |
| Owner | 人类 | 确认需求、批准风险、合并 | 把 Agent 的结论当成人工验收 |

## 交接最低格式

实现者必须提供：修改文件列表、每个修改的意图、实际运行的命令和退出码、测试输出摘要、未解决风险、是否触及高风险区域。

审查者必须给出：`PASS`、`REQUEST_CHANGES` 或 `BLOCKED`；优先级 `P0/P1/P2/P3`；文件和行号或代码位置；可复现的原因；最小修复建议。

## 变更隔离

- 一个任务一个分支或 worktree。
- 不在两个 Agent 共享的工作区里同时写同一个文件。
- 不自动 stash、reset、clean、force-push。
- 发现工作区有未提交业务改动时，先报告并避让。

## 高风险人工闸门

以下变更即使测试通过也必须人工确认：身份认证、权限、隐私/音频数据、数据库结构、核心依赖、部署配置、模型权重、删除大量文件、CI 权限和凭据。

## 共享文件安全

`.ai/`、Issue、PR 评论和 Agent 输出中禁止写入 API Key、Token、私钥、Cookie、`.env` 内容、声纹向量、原始音频、用户个人记忆正文和未脱敏的本机凭据路径。

## 争议处理

最多允许两轮“审查 → 修复”。第三轮仍不通过时标记 `BLOCKED`，由人类决定，不允许 Agent 无限循环。
