# Task 002：VoiceRuntime 生命周期与错误恢复加固

- 状态：REVIEW
- 负责人：Codex（实现、测试、验证）
- 独立审查：待 Claude Code / 人类执行；实现者不得自批准
- 创建日期：2026-08-22

## 目标

让 `VoiceRuntime` 在启动、运行、确认和关闭阶段保持可恢复、幂等、fail-closed：

1. `start()` 任一初始化步骤失败时，释放已经打开的资源，并允许后续重试。
2. `close()` 停止已挂载的 capture、TTS、dispatcher/adapter、desktop bridge，且可重复调用。
3. `say()` 的派发失败不泄漏到音频回调；状态回到可继续工作的监听态，并返回明确失败结果，不伪装成功。
4. TTS / memory 是增强项，异常不得破坏对话主链路；确认卡超时、拒绝、桥接不可用均保持拒绝。
5. 用 AUTO 测试固定生命周期和异常路径，并同步文档中的实际接线状态。

## 允许修改范围

- `vox_plugin/runtime.py`
- `tests/test_runtime.py`
- `docs/architecture.md`
- `docs/project-overview.md`
- `.ai/tasks/task-002-runtime-hardening.md`

## 禁止修改范围

- `contracts/voice-events.schema.json` 及任何契约字节/版本
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置、安全边界
- `core/`、`desktop/`、其他无关业务文件
- 既有用户未提交修改（当前工作区应先确认 clean）

## 验收标准

- 启动失败后：capture/bridge/adapter 等已创建资源均尝试清理，`_started` 为 False，后续可重试。
- 关闭后：capture 停止、TTS 停止、adapter 取消、bridge 关闭，第二次 close 不重复产生副作用。
- 派发异常后：返回 `DispatchResult(ok=False, reason=...)`，运行时保持 LISTENING，后续 turn 可继续。
- 事件仍符合现有事件契约；不修改 `voice-events.schema.json`。
- 默认 Python 回归通过；桌面/Rust 无业务改动时仍执行构建/检查。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_desktop_bridge.py -q --basetemp .pytest-run-runtime
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run
Push-Location desktop; npm run build; Pop-Location
Push-Location desktop/src-tauri; cargo check --locked; Pop-Location
```

## 证据等级

- AUTO：本地 pytest，使用 fake capture/dispatcher/TTS/bridge/adapter，不代表真实麦克风、真实 Agent 或真实 Windows 窗口。
- DOC：架构和进度文档同步。
- 本任务不产生 REAL-AGENT、REAL-EVOX、REAL-WIN 或 REAL-MIC 证据。
