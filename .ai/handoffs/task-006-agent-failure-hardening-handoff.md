# Task 006 交接：Agent 失败路径与事件隐私加固

状态：REVIEW  
分支：`codex/agent-failure-hardening`  
实现者：Codex  
独立审查：待 Claude Code / 人类执行

## 变更摘要

- `core/dispatch/dispatcher.py`
  - 对外 `task.failed` 事件不再透传 Agent 错误正文、stderr、用户提示或模型回复。
  - 仅保留固定安全摘要：`exit N`、超时、输出超限、无终结 chunk；其它情况为 `agent reported failure`。
- `core/agents/cli.py`、`core/agents/acp.py`
  - 终止/kill/wait 的每一步均 best-effort，二次超时和清理异常不掩盖原始流结果。
- `tests/test_dispatcher.py`
  - 增加事件隐私回归测试。
- `tests/test_agent_cli.py`、`tests/test_agent_acp.py`
  - 增加顽固子进程清理测试。
- 入口文档同步到 `628 passed, 3 skipped`。

## 验证证据

```text
.\.venv\Scripts\python.exe -m pytest tests\test_dispatcher.py tests\test_agent_cli.py tests\test_agent_acp.py -q --basetemp .pytest-run-agent-failure
80 passed in 2.25s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-agent-failure-full
628 passed, 3 skipped in 31.61s

git diff --check
exit code 0
```

## 审查重点

1. 公开事件是否只出现固定安全错误类别。
2. 内部 `AgentChunk.error` 与公开事件的边界是否保持清晰。
3. terminate/kill/wait 的异常是否都被隔离，且不造成错误批准或错误成功。
4. 文档基线是否与全量验证结果一致。

最高证据等级为 `AUTO`/`SIM`，没有 `REAL-AGENT`、`REAL-EVOX`、`REAL-MIC` 或 `REAL-WIN` 证据。实现者不自行写 `PASS` 或 `VERIFIED`。

