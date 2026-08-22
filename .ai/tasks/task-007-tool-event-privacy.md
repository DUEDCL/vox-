# Task 007 — 本地工具事件隐私与回调隔离

状态：REVIEW  
创建日期：2026-08-23  
实现者：Codex  
审查者：待独立审查

## 目标

加固本地工具事件出口：工具实现或第三方搜索后端抛出的异常消息不得进入公开 `tool.refused` 事件；事件 sink 自身异常不能让工具执行结果变成未处理异常。保留现有安全拒绝原因和 `tool.confirm_required` 显示命令的明确契约例外。

## 允许修改范围

- `core/tools/runner.py`
- `tests/test_tools.py`
- `tests/test_tool_security.py`
- `README.md`
- `docs/project-overview.md`
- `docs/routines.md`
- `docs/handoff.md`
- `_self_evolve/core/constitution.md`
- `_self_evolve/memory/lessons_learned.md`
- `.ai/tasks/task-007-tool-event-privacy.md`
- `.ai/handoffs/task-007-tool-event-privacy-handoff.md`

## 禁止修改范围

- `contracts/voice-events.schema.json` 和任何契约版本/字节内容
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置和安全边界
- 工具策略顺序、shell 默认关闭、确认和声纹门语义
- 禁止 `git reset --hard`、`git clean`、`git stash`、强制推送或覆盖用户修改

## 硬约束

1. `tool.confirm_required.payload.command` 仍必须展示用户即将执行的完整命令，这是唯一的内容例外。
2. `tool.refused` / `tool.executed` 不得携带文件路径、命令输出、搜索正文、异常消息或 speaker 名称。
3. 工具执行异常转为安全固定原因；事件回调异常隔离，不影响 `ToolResult` 返回和审计计数。
4. 本任务只产生 `AUTO`/`SIM` 证据，不产生真实工具或真实 Agent 证据。

## 验收标准

- 一个返回带秘密异常消息的恶意/故障工具不会把该消息写入事件。
- `ToolRunner` 的事件 sink 抛异常时，工具结果仍按成功/失败返回，且不会向调用方传播 sink 异常。
- 既有路径原因和确认命令测试保持通过。
- 工具专项与全量 Python 测试通过。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_tools.py tests\test_tool_security.py -q --basetemp .pytest-run-tool-privacy
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-tool-privacy-full
```

## 证据边界

实现者不得自行写 `PASS` 或 `VERIFIED`；独立审查仍是必需步骤。

## 实现摘要

- `ToolRunner` 对 `tool.refused` 的错误原因增加安全白名单/固定格式过滤；工具返回值仍保留给本地调用者，公开事件不再透传未知异常正文。
- `ToolRunner` 的事件 sink 进入 best-effort 模式，新增 `sink_failures` 计数；sink 崩溃不会改变工具执行结果。
- 新增测试覆盖异常消息隐私和事件 sink 故障；确认事件仍保留完整命令，既有安全拒绝原因保持不变。
- 入口文档同步当前全量基线为 `630 passed, 3 skipped`。

## 实际验证记录

```text
.\.venv\Scripts\python.exe -m pytest tests\test_tools.py tests\test_tool_security.py -q --basetemp .pytest-run-tool-privacy
125 passed, 1 skipped in 0.48s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-tool-privacy-full
630 passed, 3 skipped in 28.82s

git diff --check
exit code 0
```

当前任务仍等待独立审查；实现者不自行批准。
