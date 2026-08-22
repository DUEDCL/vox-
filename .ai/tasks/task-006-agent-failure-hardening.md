# Task 006 — Agent 失败路径与事件隐私加固

状态：REVIEW  
创建日期：2026-08-23  
实现者：Codex  
审查者：待独立审查

## 目标

加固外部 Agent 失败路径：任务失败事件不得把 Agent stderr、远端错误正文、用户提示或模型回复带入公开事件；CLI/ACP 子进程终止在超时、取消和生成器放弃时必须是 best-effort 且不抛出清理异常。

## 允许修改范围

- `core/dispatch/dispatcher.py`
- `core/agents/cli.py`
- `core/agents/acp.py`
- `tests/test_dispatcher.py`
- `tests/test_agent_cli.py`
- `tests/test_agent_acp.py`
- `README.md`
- `docs/project-overview.md`
- `docs/routines.md`
- `docs/handoff.md`
- `_self_evolve/core/constitution.md`
- `.ai/tasks/task-006-agent-failure-hardening.md`
- `.ai/handoffs/task-006-agent-failure-hardening-handoff.md`

## 禁止修改范围

- `contracts/voice-events.schema.json` 和任何契约版本/字节内容
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置和安全边界
- HTTP/EvoX 协议行为、无关 UI 和历史实验数据
- 禁止 `git reset --hard`、`git clean`、`git stash`、强制推送或覆盖用户修改

## 硬约束

1. `task.failed` 只允许固定/可证明安全的失败摘要；未知错误统一降级为不含正文的安全类别。
2. Agent 内部 `AgentChunk.error` 可以保留诊断上下文，但不能未经筛选进入公开事件。
3. 失败、超时、取消和生成器关闭均必须最终产生至多一个终结 chunk，且清理异常不能掩盖原始结果。
4. 不能把模拟子进程测试写成 `REAL-AGENT`；本任务最多产生 `AUTO`/`SIM` 证据。

## 验收标准

- 含用户提示/模型回复/Agent stderr 的错误不会出现在 `task.failed` 事件或事件 sink 中。
- `exit 3`、超时、输出超限和“无终结 chunk”等固定失败摘要仍可用于运维判断。
- CLI/ACP 的 terminate 在 terminate/kill/wait 异常及二次超时下不向调用方抛出未处理异常。
- 既有 CLI、ACP、Dispatcher 和全量测试保持通过。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_dispatcher.py tests\test_agent_cli.py tests\test_agent_acp.py -q --basetemp .pytest-run-agent-failure
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-agent-failure-full
```

## 证据边界

本任务不产生 `REAL-AGENT`、`REAL-EVOX`、`REAL-MIC` 或 `REAL-WIN` 证据；实现者不得自行写 `PASS` 或 `VERIFIED`。

## 实现摘要

- `Dispatcher` 的 `task.failed.payload.error` 现在只允许固定格式摘要：退出码、超时、输出超限和无终结 chunk；Agent 自带错误正文统一降级为 `agent reported failure`。
- 新增回归测试，证明用户提示、模型回复和 Agent stderr 不会进入公开事件。
- CLI/ACP 的终止流程拆成 terminate、首次 wait、kill、第二次 wait 四步，每一步都 best-effort；顽固子进程不会覆盖原始失败结果。
- 更新入口文档测试基线为 `628 passed, 3 skipped`；未改变真实证据边界。

## 实际验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests\test_dispatcher.py tests\test_agent_cli.py tests\test_agent_acp.py -q --basetemp .pytest-run-agent-failure
80 passed in 2.25s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-agent-failure-full
628 passed, 3 skipped in 31.61s

git diff --check
exit code 0
```

当前任务仍等待独立审查；实现者不自行批准。
