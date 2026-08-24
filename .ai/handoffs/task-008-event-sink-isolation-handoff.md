# Task 008 交接：派发、熔断与记忆事件 sink 隔离

状态：REVIEW
分支：`codex/tool-event-privacy`
实现者：Codex
独立审查：待 Claude Code / 人类执行

## 允许修改范围内的文件

- `core/dispatch/breaker.py`
- `core/dispatch/dispatcher.py`
- `core/memory/recall.py`
- `core/memory/write.py`
- `tests/test_breaker.py`
- `tests/test_dispatcher.py`
- `tests/test_memory.py`
- `README.md`
- `docs/project-overview.md`
- `docs/routines.md`
- `docs/handoff.md`
- `_self_evolve/core/constitution.md`
- `_self_evolve/memory/lessons_learned.md`
- `.ai/tasks/task-008-event-sink-isolation.md`

## 变更摘要

- 四个事件 producer 的回调都按旁路处理：sink 抛出 `Exception` 时只递增 `sink_failures`，不改变业务结果、熔断状态或本地记忆结果。
- `CircuitBreaker.describe()` 与 `MemoryWriter.describe()` 暴露失败计数；Dispatcher/MemoryRecaller 保留运行时计数属性供装配层审计。
- 事件仍先在 producer 内通过 `build_event()` 与 `validate_event()` 构造/校验；只隔离 sink 调用本身，不吞掉契约错误。
- 测试使用含秘密、路径和记忆正文的故障消息，确认这些正文不进入 producer 状态或描述结果。

## 验证证据

在 PowerShell 中使用仓库 `.venv`，并清空 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 后运行：

```powershell
$env:HTTP_PROXY=$null
$env:HTTPS_PROXY=$null
$env:ALL_PROXY=$null
$env:NO_PROXY='localhost,127.0.0.1'
.\.venv\Scripts\python.exe -m pytest tests\test_breaker.py tests\test_dispatcher.py tests\test_memory.py -q --basetemp .pytest-run-event-sinks
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-event-sinks-full
```

结果：

```text
专项：123 passed in 1.55s
全量：634 passed, 3 skipped in 30.33s
```

辅助检查：`git diff --check` 通过；`contracts/voice-events.schema.json` SHA-256 仍为 `4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`。测试目录已清理。

## 未解决风险与边界

- 本任务没有产生真实麦克风、真实 Agent、真实 EvoX 或真实 Windows 窗口证据，验证等级仅为 `AUTO`/`SIM`。
- 事件 sink 失败目前只计数，不自动重试；重试策略需要单独的架构任务，不能在本任务中隐式引入。
- 当前分支仍需独立审查；实现者不自行写 `PASS`、`VERIFIED` 或 `MERGED`。
