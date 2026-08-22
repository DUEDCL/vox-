# Task 003 交接：麦克风采集启动与回调恢复加固

- 任务状态：REVIEW
- 实现分支：`codex/capture-hardening`
- 实现者：Codex
- 独立审查：待 Claude Code / 人类；实现者不得自批准
- 创建日期：2026-08-22

## 修改文件

1. `core/audio/capture.py`
   - 将 `start()` 改为事务化初始化；KWS/ASR/声纹 provider、推理流或 `InputStream` 启动失败时尽力回滚。
   - 将 `stop()` 改为字段先摘除、资源逐项 best-effort 清理，底层 stop/close/provider close 抛错也不阻断后续复位。
   - 增加回调异常隔离；只保留异常类型名和计数，不保存异常消息、用户文本或音频。
   - ASR 回调失败后丢弃当前 ASR 流并尽力重建 KWS；KWS 重建失败时停止继续处理回调，等待显式 `stop()`。
   - 声纹验证异常仍保持 fail-closed，且拒绝通知不再携带原始异常消息。
2. `tests/test_capture_listening.py`
   - 增加启动失败重试、provider/load/create_stream 失败回滚、KWS/ASR/业务回调异常隔离、ASR reset 异常、stream stop/close 异常、二次 stop 无重复副作用等 AUTO 用例。
3. `docs/architecture.md`
   - 记录采集启动事务、幂等 best-effort 停止、回调隔离和证据等级边界。
4. `docs/project-overview.md`
   - 标注采集专项回归状态，并同步 VoiceRuntime/采集加固进度。
5. `.ai/tasks/task-003-capture-hardening.md`
   - 状态更新为 `REVIEW`，补充实现摘要。

## 实际验证

- `.\.venv\Scripts\python.exe -m pytest tests\test_capture_listening.py tests\test_speaker_privacy.py -q --basetemp .pytest-run-capture`
  - 结果：`36 passed`，退出码 0。
  - 证据等级：AUTO；使用 stub provider / fake InputStream，不代表真实麦克风。
- `.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run`
  - 结果：`619 passed, 3 skipped`，退出码 0。
- `git diff --check`
  - 结果：通过。
- `Get-FileHash -Algorithm SHA256 contracts\voice-events.schema.json`
  - 结果：`4f60b6124dcb9704624a0606f411981d0bf572de22fcf4a25fad133bd3c75de5`；本任务未改动该文件。

## 审查边界与风险

- 本任务只产生 AUTO/DOC 证据，不产生 REAL-MIC、REAL-AGENT、REAL-EVOX 或 REAL-WIN。
- 尚未进行真实设备开合、sounddevice 原生回调线程、声纹本人/他人拒绝和长时间运行测试。
- 未修改事件契约、enrollment、`.env`、凭据、原始音频、模型权重、数据库结构、核心依赖、部署配置或安全边界。
- 该交接不是独立审查结论；不能写 `PASS` 或 `VERIFIED`，需独立审查者复核 diff 并附实际命令结果。

## 建议审查命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_capture_listening.py tests\test_speaker_privacy.py -q --basetemp .pytest-run-capture
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run
git diff --check
```
