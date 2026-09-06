# 仓库文档分层

本页说明哪些文件面向 GitHub 使用者，哪些文件只服务于本机开发工具，以及为什么它们会出现在或不再出现在 GitHub 的 `main` 分支。

## 公开仓库内容

| 文件 | 读者 | 作用 |
|---|---|---|
| `README.md` | 使用者、贡献者 | 项目定位、隐私边界、安装、启动和验证入口 |
| `docs/` | 使用者、维护者、贡献者 | 架构、需求、测试、交接、研究记录和 ADR |
| `AGENTS.md` | 编码 Agent、贡献者 | 唯一公开协作规则入口 |
| `CLAUDE.md` | Claude Code | 兼容入口，只跳转到 `AGENTS.md` |

## 本机文件

以下内容保留在本机，但不应进入 GitHub：

| 路径 | 原因 |
|---|---|
| `.claude/` | Claude Code 的本机规则、权限、启动配置和历史归档；可能含机器路径或开发环境细节 |
| `.codex/`、`.agents/` | 本机 Agent 工具状态和技能缓存 |
| `.agent-workspace/`、`.agent-scratch/`、`.open-design-review/` | Agent 隔离工作区、临时草稿和设计审查产物 |
| `.playwright-mcp/` | 浏览器自动化调试快照 |
| `.env`、`memory/`、`enrollment/`、`models/` | 凭据、个人记忆、生物特征和模型权重 |
| `bot-*.png`、`desktop/life-*.png`、`desktop/seq-*.png` | 唤醒球视觉取证截图；正式证据应整理后放到 `docs/assets/` |

这些规则由 `.gitignore` 负责。忽略规则只对未跟踪文件生效；已经被 Git 跟踪的文件必须通过一次明确的索引清理才能从后续提交中移除。

## 为什么之前 GitHub `main` 里有 `CLAUDE.md`

GitHub 展示的是 Git 已提交的文件。只要文件曾被 `git add`、提交并推送到 `main`，它就会出现在仓库页面；GitHub 不会因为文件名是 `CLAUDE.md` 而自动生成它。

本仓库之前跟踪过：

```text
CLAUDE.md
.claude/CLAUDE.md
.claude/launch.json
.claude/settings.json
```

其中 `.claude/CLAUDE.md` 是 Claude Code 的项目级规则，`launch.json` 和 `settings.json` 是本机开发配置。它们不是 Vox 的运行时依赖，也不应被误认为产品文档。

本次整理后的职责是：

- `README.md` 和 `docs/` 负责公开产品信息。
- `AGENTS.md` 负责公开且可复用的协作规则。
- `CLAUDE.md` 只保留兼容入口。
- `.claude/` 保留在本机并整体忽略，不再污染 GitHub 主线。

旧的详细 Claude 项目规则已在本机保存为 `.claude/CLAUDE.legacy-20260906.md`，没有随本次提交上传。

## 后续提交前检查

```powershell
git status --short
git diff --stat
git diff --check
```

看到 `.claude/`、`.playwright-mcp/`、Agent 临时目录或取证截图时，先确认它们是否已经被 `.gitignore` 忽略；正式素材应改名、放入明确的产品或文档目录后再提交。