# Task 005 — 文档基线与当前进度同步

状态：REVIEW  
创建日期：2026-08-23  
实现者：Codex  
审查者：待独立审查

## 目标

同步项目入口文档中的过期测试基线、阶段状态、提交信息和 DesktopBridge/VoiceRuntime/采集生命周期进度，避免接手者根据陈旧信息做错误判断。只修正文档，不借机改变代码、契约或发布安全边界。

## 允许修改范围

- `README.md`
- `docs/project-overview.md`
- `docs/routines.md`
- `docs/handoff.md`
- `_self_evolve/core/constitution.md`
- `_self_evolve/memory/lessons_learned.md`
- `.ai/tasks/task-005-docs-truth-sync.md`
- `.ai/handoffs/task-005-docs-truth-sync-handoff.md`

## 禁止修改范围

- Python、TypeScript、Rust 及测试实现代码
- `contracts/voice-events.schema.json` 和任何契约版本/字节内容
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置和安全边界
- 不相关文档、历史实验数据和已记录的真实证据
- 禁止 `git reset --hard`、`git clean`、`git stash`、强制推送或覆盖用户修改

## 硬约束

1. 当前可复现实测基线使用仓库隔离环境：`625 passed, 3 skipped`。
2. DesktopBridge 专项为 `33 passed`；之前的 capture 专项 `36 passed` 仍按已有交接证据记录，但不能拼接成单个专项数字。
3. `npm run build`、`cargo check` 的证据等级为 `AUTO`；替身 Agent/桌面进程测试只能是 `SIM`。
4. 不得把代码级接线写成 `REAL-WIN`、`REAL-AGENT`、`REAL-EVOX` 或 `REAL-MIC`。
5. 不删除或重写 `docs/research/prototype-results.md` 中的历史实验记录；如需修正历史事实，必须保留原证据与修订原因。

## 验收标准

- README 不再声称 Phase 4 尚未开始，且入口测试基线与当前证据一致。
- `docs/project-overview.md` 的更新时间、测试数量、提交快照、P8/P9/P10 状态与当前仓库一致。
- `docs/routines.md` 和 `docs/handoff.md` 的全量测试命令/基线不再引用 `518`、`600` 等陈旧数字；其证据等级说明保持诚实。
- `_self_evolve/core/constitution.md` 的进度快照反映当前分支完成的工作，同时保留真实验收缺口。
- 文档变更通过 `git diff --check`，且不触及代码/契约/敏感路径。

## 验证命令

```powershell
git diff --check
rg -n "518 passed|600 passed|619 passed|Phase 4（生产实现）尚未开始|Phase 4\(生产实现\)尚未开始" README.md docs _self_evolve
```

第二条命令允许只命中历史实验/交接解释中的“旧数字”说明；不能再把它们作为当前基线使用。

## 证据边界

本任务只产生 `DOC`/`AUTO` 证据，不产生任何 REAL 级证据。实现者不得自行写 `PASS` 或 `VERIFIED`。

## 实现摘要

- 更新 `README.md`、`docs/project-overview.md`、`docs/routines.md`、`docs/handoff.md` 与 `_self_evolve/core/constitution.md` 的当前基线和阶段描述。
- 将当前全量基线统一为 `625 passed, 3 skipped`，DesktopBridge 专项统一为 `33 passed`，并保留采集专项 `36 passed` 的独立证据边界。
- 补充最近的 VoiceRuntime、采集生命周期和 DesktopBridge 生命周期加固进度；保留真实 Agent、EvoX、麦克风和 Windows 窗口未验收状态。
- 将例程中的 Python 测试命令统一指向仓库 `.venv`，避免系统 Python 造成错误基线。

## 实际验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-doc-sync
625 passed, 3 skipped

git diff --check
exit code 0

rg -n "518 passed|600 passed|619 passed|622 用例|Phase 4（生产实现）尚未开始|Phase 4\\(生产实现\\)尚未开始" README.md docs _self_evolve
仅命中 docs/research/prototype-results.md 的历史实验记录（未修改）。
```

直接不带 `--basetemp` 的同一测试命令在本机也完成了 625 个通过用例，但在 pytest 清理默认临时目录时遇到 `WinError 5`；因此文档和基线采用仓库隔离临时根目录，避免把清理权限问题误判成测试回归。代码、契约和敏感路径未改动；当前任务仍等待独立审查。
