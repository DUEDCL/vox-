# Task 004 交接：DesktopBridge 生命周期与并发边界加固

状态：REVIEW  
分支：`codex/desktop-bridge-hardening`  
实现者：Codex  
独立审查：待 Claude Code / 人类执行

## 变更文件

- `core/desktop_bridge.py`
  - 增加会话代数，隔离旧 reader 与新进程。
  - 支持 `close()`/自然 EOF 后重新 `start()`。
  - 启动失败、reader 启动失败和子进程退出时回滚状态并拒绝 pending confirmations。
  - 用锁保护检查-写入操作，避免 `send()` 与 `close()` 的管道竞态。
  - reader 自己关闭 stdout；回调异常不影响清理。
- `tests/test_desktop_bridge.py`
  - 覆盖 close 后重启、Popen/reader 启动失败回滚、EOF 状态复位、旧 reader 未 EOF 时重启释放等待者、并发写入关闭。
  - 保持替身进程证据等级为 `SIM`。
- `.ai/tasks/task-004-desktop-bridge-hardening.md`
  - 记录允许范围、验收标准、验证命令、证据边界和未解决风险。

## 验证证据

实现者实际执行：

```text
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_bridge.py -q --basetemp .pytest-run-desktop-bridge2
33 passed in 2.18s

.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-full-final3
625 passed, 3 skipped in 28.64s

Push-Location desktop; npm run build; Pop-Location
exit code 0

Push-Location desktop/src-tauri; cargo check; Pop-Location
exit code 0
```

契约 `contracts/voice-events.schema.json` 未修改，SHA-256：
`4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`。

## 审查边界

- 当前没有 `PASS`/`VERIFIED` 结论；实现者不能批准自己的实现。
- 当前证据最高为 `AUTO`/`SIM`。
- `REAL-WIN` 透明窗口和硬件环境验收仍未完成。
- `REAL-AGENT`、`REAL-EVOX`、`REAL-MIC` 仍未完成。

## 审查建议

独立审查者应重点检查：

1. 旧 reader 在重启后是否无法清理新会话的 `ready`/confirmation 状态。
2. 启动失败后 process、reader 和 ready 是否均复位且可以重试。
3. close、EOF、隐藏和发送失败之间是否始终 fail-closed。
4. `describe()` 是否只包含计数/状态，不包含事件正文、命令、异常消息或路径。

