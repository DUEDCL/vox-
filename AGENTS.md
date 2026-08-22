# Vox：Codex / Claude Code 协作规则

本文件是 Codex 在本仓库中的入口规则。先阅读 `.ai/CONTRACT.md`，再阅读已有项目规则 `.claude/CLAUDE.md`。

## Agent 分工

- **Claude Code**：架构分析、任务拆解、独立审查、风险识别。
- **Codex**：具体实现、测试编写、验证执行、根据审查意见修复。
- **人类**：确认需求、处理争议、批准高风险变更、最终合并。

实现者不得批准自己的实现。任何 `PASS` 都必须来自独立审查，并附带实际验证命令和结果。

## 不可违反的项目约束

1. 遵守 `.claude/CLAUDE.md` 的本地优先、组件可替换、证据等级和安全红线。
2. 不得把低等级验证写成高等级验证；模拟 Agent 子进程只算 `SIM`，不能声称 `REAL-AGENT`。
3. `contracts/voice-events.schema.json` 字节内容和版本不得被修改；平台事件使用已有 Agent 契约。
4. 不得读取、提交或修改 `enrollment/`、`.env`、凭据、声纹文件和模型权重。
5. 不得执行 `git reset --hard`、`git clean`、强制推送或覆盖现有未提交修改。
6. 不得擅自改变数据库结构、核心依赖、部署配置或安全边界。
7. 每个任务必须声明允许修改范围、禁止修改范围、验收标准和验证命令。

## 验证命令

默认使用仓库隔离环境：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

按改动范围选择 `.claude/CLAUDE.md` 中的最小回归命令；项目层改动收尾时再运行全量测试。桌面改动需额外运行：

```powershell
Push-Location desktop
npm run build
Pop-Location
Push-Location desktop/src-tauri
cargo check
Pop-Location
```

## Git 约束

- 每个任务使用独立分支或 Git worktree。
- 当前工作区已有用户改动时，Agent 只能保留并避让，不能 stash、reset、clean 或覆盖。
- 合并前必须能回答：改了哪些文件、为什么改、跑了哪些命令、结果是什么、还有什么风险。

## 协作产物

- 任务：`.ai/tasks/task-*.md`
- 审查：`.ai/reviews/task-*-review.md`
- 交接：`.ai/handoffs/task-*-handoff.md`
- 统一协议：`.ai/CONTRACT.md`

如果任务文件不存在，先创建任务说明，不要直接进行大规模修改。
