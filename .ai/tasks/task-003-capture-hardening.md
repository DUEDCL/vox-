# Task 003：麦克风采集启动与回调恢复加固

- 状态：REVIEW
- 负责人：Codex（实现、测试、验证）
- 独立审查：待 Claude Code / 人类执行；实现者不得自批准
- 创建日期：2026-08-22

## 目标

完善 `SounddeviceWakeCapture` 的真实设备生命周期，避免设备启动失败或音频回调异常后留下半初始化状态：

1. `start()` 采用事务化初始化，`InputStream.start()` 或模型加载失败后释放已创建的流与 provider，并允许重试。
2. 音频回调中的 KWS/ASR/业务回调异常不得冒出 sounddevice 线程；记录安全的异常类型并尽力恢复到可继续监听的 KWS 状态。
3. `stop()` 幂等且 best-effort，即使底层 stream 的 stop/close 抛错，也要清理内部状态和 provider。
4. 不改变既有事件契约，不落盘、不上传音频，不放宽声纹 fail-closed 边界。

## 允许修改范围

- `core/audio/capture.py`
- `tests/test_capture_listening.py`
- `docs/architecture.md`
- `docs/project-overview.md`
- `.ai/tasks/task-003-capture-hardening.md`
- `.ai/handoffs/task-003-capture-hardening-handoff.md`

## 禁止修改范围

- `contracts/voice-events.schema.json` 及任何契约字节/版本
- `enrollment/`、`.env`、凭据、声纹文件、原始音频、模型权重
- 数据库结构、核心依赖、部署配置、安全边界
- 既有用户未提交修改；不得 stash、reset、clean 或强制推送

## 验收标准

- 设备流启动失败后，下一次 `start()` 可以重新尝试；内部 stream/inference/ASR 状态不残留。
- 回调异常被捕获，`callback_errors` 递增，错误对外只暴露异常类型；后续回调仍可工作或明确保持安全停止态。
- `stop()` 可重复调用，底层资源最多执行一次有效清理；所有内部状态复位。
- 既有 capture、speaker privacy、runtime 和全量 Python 测试通过。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_capture_listening.py tests/test_speaker_privacy.py -q --basetemp .pytest-run-capture
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run
```

## 实现摘要

- `core/audio/capture.py`：启动改为事务化；失败时 best-effort 回收 stream、KWS/ASR/声纹 provider 并复位字段；`stop()` 先摘除资源再逐项清理，保持幂等。
- `core/audio/capture.py`：音频回调统一隔离异常，记录 `callback_errors` 与不含消息的 `last_callback_error` 类型名；ASR/KWS 失败后尽力恢复 KWS，恢复失败时停止处理后续回调并等待显式 `stop()`。
- `tests/test_capture_listening.py`：增加设备启动失败重试、provider 部分初始化、KWS/ASR/业务回调异常、底层 stop/close 异常和二次 stop 等 AUTO 用例。
- `docs/architecture.md`、`docs/project-overview.md`：同步采集生命周期、回调异常隔离和 AUTO/REAL-MIC 证据边界。

## 证据等级

- AUTO：stub stream/KWS/ASR，不代表真实麦克风。
- 本任务不产生 REAL-MIC、REAL-AGENT、REAL-EVOX 或 REAL-WIN 证据。
