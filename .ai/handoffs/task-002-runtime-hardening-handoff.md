# Task 002：VoiceRuntime 生命周期与错误恢复交接

- 任务：`.ai/tasks/task-002-runtime-hardening.md`
- 当前状态：REVIEW
- 实现分支：`codex/runtime-hardening`
- 交接日期：2026-08-22
- 实现者：Codex
- 独立审查：尚未完成；实现者未批准自身实现

## 变更文件

- `vox_plugin/runtime.py`
  - `start()` 变为可回滚初始化事务；失败后清理已创建资源、保持 `_started=False`，允许再次启动。
  - `close()` 进行 best-effort、幂等资源清理；适配器、dispatcher、工具、memory、capture、TTS、transport 和 desktop bridge 均按已支持的 teardown hook 清理。
  - teardown 每个资源只选择一个可用的 `stop`/`close`/`shutdown` 方法，避免同一资源重复产生副作用。
  - close 后保留注入的 capture/TTS 挂载，使同一个 runtime 可以重新 `start()`；资源本身已停止。
  - 派发、确认桥接和回合完成异常均保持拒绝/失败语义并恢复到 `LISTENING`。
  - `task.failed` 事件使用 `agent-events.schema.json` 校验，payload 只带 `task_id` 与异常类型，不带用户原文或回复。
- `tests/test_runtime.py`
  - 增加启动回滚、失败重试、关闭幂等、close 后重启、派发异常、确认桥接异常、TTS/memory 增强项异常和回合完成异常测试。
- `docs/architecture.md`
  - 同步 VoiceRuntime 组合根、实际派发链路、生命周期和证据等级说明。
- `docs/project-overview.md`
  - 同步 P6/P8 接线状态、测试基线和未完成真实验收项。
- `.ai/tasks/task-002-runtime-hardening.md`
  - 状态从 `IN_PROGRESS` 更新为 `REVIEW`。

## 验证命令与结果

- `D:\program\vioce-wake\.venv\Scripts\python.exe -m pytest tests\test_runtime.py tests\test_desktop_bridge.py -q --basetemp .pytest-run-runtime-final3`
  - 结果：`41 passed`，退出码 0，证据等级 AUTO。
- `D:\program\vioce-wake\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run-full-final`
  - 结果：`607 passed, 3 skipped`，退出码 0，证据等级 AUTO。
- `Push-Location desktop; npm run build; Pop-Location`
  - 结果：Vite build 成功，退出码 0。
- `Push-Location desktop\src-tauri; cargo check --locked; Pop-Location`
  - 结果：成功，退出码 0。
- `git diff --check`
  - 结果：通过。
- `contracts/voice-events.schema.json` 保护检查
  - 工作区 blob 与 HEAD 一致；SHA-256 为 `4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`。

## 未解决风险与边界

- 尚无独立 Claude Code / 人类审查报告；不能标记 `VERIFIED` 或 `PASS`。
- 本任务只产生 AUTO/DOC 证据，不产生 REAL-MIC、REAL-AGENT、REAL-EVOX、REAL-WIN 证据。
- `npm` 依赖审查仍有此前记录的 `1 moderate, 1 high` 漏洞；本任务未运行 `npm audit fix`，未修改依赖配置。
- 真实 Agent、真实 EvoX 桥、真实 Windows 窗口、真实麦克风/TTS 播放和长时间资源画像仍需后续验收。
- `README.md` 仍有部分旧阶段描述；本任务允许范围未包含 README，因此未修改。

## 后续动作

1. 独立审查者读取本分支 diff，写入 `.ai/reviews/task-002-runtime-hardening-review.md`。
2. 若审查请求修改，Codex 只修复明确问题并重新执行最小回归。
3. 审查通过且人类批准后，再合并到目标分支。
4. 当前分支可安全推送到 `origin/codex/runtime-hardening`，禁止强制推送。