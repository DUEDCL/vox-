# Task 004 — DesktopBridge 生命周期与并发边界加固

状态：REVIEW
创建日期：2026-08-22
实现者：Codex
审查者：待独立审查

## 目标

在不改变桌面 IPC 协议、事件契约或安全边界的前提下，加固 `DesktopBridge` 的进程生命周期、重启、EOF/异常退出和并发关闭行为，使确认等待在所有桥接失效路径上都 fail-closed，且不会遗留旧进程/线程状态。

## 允许修改范围

- `core/desktop_bridge.py`
- `tests/test_desktop_bridge.py`
- `.ai/tasks/task-004-desktop-bridge-hardening.md`
- `.ai/handoffs/task-004-desktop-bridge-hardening-handoff.md`
- 如确有必要，仅同步相关架构/进度文档；本任务默认不改桌面前端和 Rust。

## 禁止修改范围

- `contracts/voice-events.schema.json` 及任何契约版本/字节内容
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置和安全边界
- 不相关模块和既有用户改动
- 禁止 `git reset --hard`、`git clean`、`git stash`、强制推送和覆盖未提交修改

## 设计约束

1. 出站事件仍必须经过 `validate_any_event`。
2. 事件发送失败是 best-effort；确认批准只能来自明确的 `approved is True`，其它情况均拒绝。
3. `close()` 必须幂等，并释放所有等待者；隐藏桌面时挂起确认必须拒绝。
4. 不能把事件正文、命令、异常消息、路径或用户文本写入 `describe()`。
5. 真实桌面验收仍标注为 `REAL-WIN` 未完成；子进程替身测试只能标注 `SIM`。

## 验收标准

- 同一实例 `close()` 后可安全重新 `start()`，且新会话的 `ready` 状态不继承旧会话。
- 子进程 EOF/异常退出后，`process`、`ready`、reader 状态和 pending confirmations 可观察地复位；确认等待及时拒绝。
- `start()` 任一步失败时不遗留 process、reader 或 ready 状态，后续可重试。
- `send()`、`set_visible()`、`await_confirmation()` 与 `close()` 竞态不会抛出未处理异常，不会错误批准确认。
- 回调异常被隔离，诊断不泄漏事件内容。

## 最小验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_bridge.py tests\test_runtime.py -q --basetemp .pytest-run-desktop-bridge
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run
```

若只修改 Python，不强制执行桌面构建；若收尾条件允许，再运行：

```powershell
Push-Location desktop
npm run build
Pop-Location
Push-Location desktop/src-tauri
cargo check --locked
Pop-Location
```

## 证据边界

本任务最多产生 `AUTO`/`SIM` 证据，不产生 `REAL-WIN`、`REAL-AGENT`、`REAL-EVOX` 或 `REAL-MIC` 证据。

## 实现摘要

- `DesktopBridge` 以会话代数隔离 reader，支持 `close()`/EOF 后重新启动，旧 reader 不能修改新会话状态。
- 子进程 EOF、关闭和启动失败都会清理可观察状态，并将 pending confirmation 结算为拒绝。
- 启动过程中 Popen 或 reader 线程失败会回滚子进程、reader 和 `ready` 状态，后续可重试。
- 写入在同一锁内完成进程检查与管道写入，降低 `send()`/`close()` 竞态；确认槽位防止重复结算。
- reader 自己负责关闭 stdout，避免继承管道的后代导致关闭线程阻塞；回调异常仍被隔离。

## 实际验证记录

以下命令由实现者执行，不能替代独立审查：

```text
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_bridge.py -q --basetemp .pytest-run-desktop-bridge2
33 passed in 2.18s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-full-final3
625 passed, 3 skipped in 28.64s

Push-Location desktop; npm run build; Pop-Location
exit code 0

Push-Location desktop/src-tauri; cargo check; Pop-Location
exit code 0

git diff --check
exit code 0
```

契约文件 `contracts/voice-events.schema.json` 未出现在 diff 中；其 SHA-256 仍为：

```text
4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5
```

## 未解决风险

- 尚未进行真实透明窗口、DPI、多显示器、托盘、焦点和拖动验收（`REAL-WIN`）。
- 尚未进行真实 Agent、真实麦克风和真实 EvoX 联调。
- 本文件和测试中的子进程均为 `SIM`；不应写成 `REAL-AGENT` 或 `REAL-WIN`。

## 审查请求

请独立检查 `core/desktop_bridge.py` 与 `tests/test_desktop_bridge.py` 的生命周期、线程竞态和 fail-closed 语义，运行实际验证命令，并在 `.ai/reviews/task-004-desktop-bridge-hardening-review.md` 给出 `PASS`、`REQUEST_CHANGES` 或 `BLOCKED`。实现者不自行批准本任务。

