# Task 008 — 派发、熔断与记忆事件 sink 隔离

状态：REVIEW
创建日期：2026-08-24
实现者：Codex
审查者：待独立审查

## 目标

统一剩余平台事件 producer 的回调故障语义：`CircuitBreaker`、`Dispatcher`、`MemoryWriter` 和 `MemoryRecaller` 的 `on_event`/`_on_event` 是旁路出口，sink 抛异常不得破坏熔断状态、派发结果或记忆读写结果。保留事件在 producer 内构造并按现有契约校验；只隔离回调调用本身，不吞掉 producer 自己的契约/业务错误。

## 允许修改范围

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
- `.ai/handoffs/task-008-event-sink-isolation-handoff.md`

## 禁止修改范围

- `contracts/voice-events.schema.json` 和任何契约版本/字节内容
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置和安全边界
- 路由、工具策略、确认、声纹门和 agent 失败摘要语义
- 禁止 `git reset --hard`、`git clean`、`git stash`、强制推送或覆盖用户修改

## 验收标准

1. 四个 producer 的 sink 抛出 `Exception` 时，原有 API 结果和状态转移保持不变。
2. 每个 producer 都提供可审计的 sink 失败计数，且不会记录异常正文或任务/记忆文本。
3. 没有 sink 时仍执行原有事件构造和契约校验。
4. 既有事件顺序、payload 和安全边界测试保持通过。
5. 只声明 `AUTO`/`SIM` 证据，不把模拟 sink 或 agent 验证写成真实设备/真实 Agent 验证。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_breaker.py tests\test_dispatcher.py tests\test_memory.py -q --basetemp .pytest-run-event-sinks
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-event-sinks-full
```

## 实际实现摘要

- `CircuitBreaker`、`Dispatcher`、`MemoryWriter`、`MemoryRecaller` 的 sink 调用均改为 best-effort。
- 每个 producer 维护 `sink_failures`；熔断器和记忆 writer 的 `describe()` 也公开计数，异常正文不落入状态或诊断结构。
- 新增四条回归路径：sink 失败不改变熔断状态、派发成功结果、记忆写入结果或记忆召回结果。

## 实际验证记录

```text
专项：123 passed in 1.55s
全量：634 passed, 3 skipped in 30.33s
全量运行时清空 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，避免本机代理拦截 loopback 测试
git diff --check：通过
voice-events.schema.json SHA-256：4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5
```

## 证据边界

实现者不得自行写 `PASS` 或 `VERIFIED`；独立审查仍是必需步骤。以上仅为 `AUTO` 测试证据，不包含 REAL-MIC、REAL-AGENT、REAL-EVOX 或 REAL-WIN 证据。
