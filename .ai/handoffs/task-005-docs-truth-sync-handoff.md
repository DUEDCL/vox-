# Task 005 交接：文档基线与当前进度同步

状态：REVIEW  
分支：`codex/docs-truth-sync`  
实现者：Codex  
独立审查：待 Claude Code / 人类执行

## 变更文件

- `README.md`：更新项目阶段、模块入口、测试基线和 Git 说明。
- `docs/project-overview.md`：更新单一事实来源中的测试数量、最近进度、P8 状态和已关闭的历史接线缺口。
- `docs/routines.md`：统一仓库隔离 Python 命令，更新全量、DesktopBridge 和采集专项基线。
- `docs/handoff.md`：更新交接基线、DesktopBridge 验证证据和真机验收入口。
- `_self_evolve/core/constitution.md`：更新认知核心的进度快照与日期。
- `_self_evolve/memory/lessons_learned.md`：记录 Windows 默认 pytest 临时目录清理权限问题及隔离验证策略。
- `.ai/tasks/task-005-docs-truth-sync.md`：任务范围、验收标准和验证记录。

## 验证证据

```text
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-doc-sync
625 passed, 3 skipped

git diff --check
exit code 0
```

旧的 `600 passed` 只在未修改的 `docs/research/prototype-results.md` 历史实验记录中保留，不再作为当前基线使用。直接默认临时目录清理曾在测试完成后触发 `WinError 5`，因此交接命令固定使用仓库隔离的 `--basetemp`。当前文档不宣称 `REAL-MIC`、`REAL-AGENT`、`REAL-EVOX` 或 `REAL-WIN` 已完成。

## 审查重点

1. 文档中的 `625 / 3`、`33`、`36` 是否分别对应全量、DesktopBridge、采集专项，而非错误相加。
2. P8/P9/P10 和发布阻塞项是否仍保持真实证据边界。
3. 文档变更是否意外修改了历史实验结论、契约约束或敏感信息。
4. 任务文件中的验证命令和结果是否可复现。

实现者不自行写 `PASS` 或 `VERIFIED`；请独立审查者在 `.ai/reviews/task-005-docs-truth-sync-review.md` 给出结论。
