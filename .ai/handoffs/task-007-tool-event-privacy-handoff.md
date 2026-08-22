# Task 007 交接：本地工具事件隐私与回调隔离

状态：REVIEW  
分支：`codex/tool-event-privacy`  
实现者：Codex  
独立审查：待 Claude Code / 人类执行

## 变更摘要

- `core/tools/runner.py`
  - 工具实现/搜索后端的未知错误不会进入 `tool.refused` 事件。
  - 事件原因只保留安全固定原因或固定格式（如退出码、超时、已知策略拒绝）。
  - 事件 sink 异常被隔离，并通过 `sink_failures` 计数，不影响 `ToolResult`。
- `tests/test_tools.py`
  - 增加恶意工具异常消息不泄露到事件的测试。
  - 增加 sink 崩溃不影响工具成功结果的测试。
- 入口文档同步全量基线为 `630 passed, 3 skipped`。

## 验证证据

```text
.\.venv\Scripts\python.exe -m pytest tests\test_tools.py tests\test_tool_security.py -q --basetemp .pytest-run-tool-privacy
125 passed, 1 skipped in 0.48s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-tool-privacy-full
630 passed, 3 skipped in 28.82s

git diff --check
exit code 0
```

`tool.confirm_required` 继续保留命令原文，这是 FR-6.13 的明确安全例外；没有新增真实设备、Agent 或网络证据。实现者不自行写 `PASS` 或 `VERIFIED`。

