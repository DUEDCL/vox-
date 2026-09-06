# Vox 仓库协作规则

本文件是仓库内**唯一的公开 Agent 协作规则入口**。面向使用者的内容放在 `README.md` 和 `docs/`；Claude Code 的兼容入口 `CLAUDE.md` 只引用本文件。本机 Agent 配置放在 `.claude/`、`.codex/` 等忽略目录，不提交到 GitHub。

## 项目边界

1. **本机准入、处理可配置**
   - KWS（关键词唤醒）、VAD（语音活动检测）、声纹注册与校验在本机执行。
   - 通过准入后的语音是否发送到云端 ASR（语音识别）/TTS（语音合成），由 `config/voice.toml` 决定。
   - 音频不写入记忆库；`enrollment/`、`memory/`、`models/` 和 `.env` 不进版本控制。
2. **组件可替换**
   - KWS、ASR、TTS、agent、MCP、会话桥接和桌面 UI 都在配置或版本化契约之后。
   - 不让第三方 SDK 类型泄漏进公开事件结构。
3. **安全边界默认收紧**
   - 密钥只从环境变量或本机 `.env` 读取，不写进代码、版本化配置、日志或回复。
   - 不降低控制台回环绑定、token 校验、工具白名单、确认门、声纹门和 URL 凭据校验。
4. **证据等级诚实**
   - DOC < AUTO < SIM < REAL-MIC < REAL-AGENT < REAL-EVOX < REAL-WIN。
   - mock、录音回放和代码级接线不能写成真机验证。

## 开工前

执行并确认：

```powershell
git status --short
git branch --show-current
git log --oneline -3
```

判据：

- 在 `main`；
- 最新提交符合预期；
- 工作区若不干净，先辨认已有改动，保留并避让，不覆盖、不顺手提交。

这个仓库采用一条 `main` 主线。不开长期分支，不用 worktree 并行写同一仓库，不恢复旧的双 Agent 接力流程。

## 修改原则

- 先读相关代码和测试，再说明方案，再修改。
- 修 bug 先写复现测试；加功能先写清验收判据。
- 优先定点修改，不顺手重构，不增加需求之外的抽象和配置。
- 通用能力先调查可本地/离线运行的成熟实现，再决定引入还是手写；优先看架构侵入性和许可证。
- 写入前读取目标文件。发现不属于当前任务的改动时避让，不覆盖。
- 未经使用者确认，不执行 `reset --hard`、`clean -f`、`push --force`、`stash`、递归删除或批量移动。

## 常用验证

Python 全量测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp ".pytest-agent-$PID"
```

当前机器的系统 pytest 临时目录可能有权限残留，使用仓库内独立 `--basetemp` 可避免把环境权限错误误判为测试失败。测试结束后的 `.pytest-*` 已被忽略。

按改动范围补跑：

```powershell
# 桌面前端
Push-Location desktop
npm run build
Pop-Location

# Tauri/Rust
Push-Location desktop/src-tauri
cargo check
Pop-Location
```

真机、外部 agent、EvoX 和透明窗口验收入口见 `docs/testing.md` 与 `docs/routines.md`，不能用 AUTO/SIM 结果替代。

## Git 与提交

- Conventional Commits：`feat|fix|docs|test|refactor|chore(scope): 描述`。
- 一次会话只提交当前任务明确涉及的文件；已有无关改动留在工作区。
- 提交正文写清：改了什么、为什么、执行了哪些验证及结果。
- 不添加 `Co-Authored-By` trailer。
- 只有测试与 diff 复核完成后才推送项目 GitHub 的 `main`。

## 给使用者的命令

使用者在 Windows PowerShell 5.1 中执行命令：

- 使用 `;`，不要给出依赖 `&&` 的命令。
- 使用 `.\.venv\Scripts\python.exe`，不要写 Unix 风格路径。
- 环境变量先单独设置，例如 `$env:PYTHONUTF8=1`。
- 使用 `Get-Content`、`Select-String` 等 PowerShell 命令，不要求用户安装 `cat`、`grep`。

## 文档职责

- `README.md`：项目定位、隐私边界、安装、启动和验证入口。
- `docs/architecture.md`：架构与组件边界。
- `docs/testing.md`、`docs/routines.md`：测试矩阵与可重复验证步骤。
- `docs/backlog.md`：已识别但故意未完成的工作。
- `docs/adr/`：架构决策及其取代关系。
- `docs/repository-guide.md`：公开文档和本机 Agent 文件的分层说明。

不要把长期有效的产品事实只写进本机 Agent 配置；应同步到对应的公开文档或 ADR。
