# Vox：Claude Code 协作入口

@.claude/CLAUDE.md
@.ai/CONTRACT.md

## 本仓库的双 Agent 流程

Claude Code 默认承担架构师和独立审查员角色；Codex 默认承担实现者和修复者角色。每次任务必须经过：

```text
任务说明 → 实现 → 独立审查 → 修复 → 验证 → 人工合并
```

审查阶段不要直接编辑代码；审查结果写入 `.ai/reviews/`。只有在读取审查结果后，Codex 才可以进入修复阶段。

不要覆盖已有未提交修改，不要降低本项目的本地隐私、安全和验证等级约束。
