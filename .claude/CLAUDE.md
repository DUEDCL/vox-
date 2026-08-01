# EvoX Voice Wake — 项目规则

全局准则见 `~/.claude/CLAUDE.md`。本文件只写本项目特有的约束。

## 三条设计红线(不得违反)

1. **本地优先** — 唤醒、VAD（语音活动检测）、TTS（语音合成）全部本机执行；项目代码不保存、不上传音频。新增依赖若含云调用或 telemetry（遥测），直接否决。
2. **组件可替换** — KWS/ASR/TTS/会话传输都在契约之后。任何 `sherpa-onnx`、`sounddevice`、VoxCord 的类型都不得出现在 `contracts/voice-events.schema.json` 或公开事件结构里（`additionalProperties: false` 是第一道闸门）。
3. **验证等级诚实** — 六级证据 DOC < AUTO < SIM < REAL-MIC < REAL-EVOX < REAL-WIN。禁止把低等级当高等级用，禁止把 mock（模拟）验证写成真机验收。声明结论必须标注等级。

## 改完什么跑什么

完整例程见 `docs/routines.md`。最小回归对照表：

| 改动范围 | 命令 | 期望 |
|---|---|---|
| `core/` `evox_plugin/` | `.venv\Scripts\python.exe -m pytest tests -q` | 19 passed, 2 skipped |
| `contracts/` 或事件结构 | `pytest tests/test_event_schema.py tests/test_voice_contract.py tests/test_plugin_tools.py -q` | 全绿 |
| `core/providers.py` | `pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q` | 全绿 |
| `core/session_bridge.py` | `pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q` | 全绿 |
| `desktop/src/` | `cd desktop && npm run build` | tsc + vite 通过 |
| `desktop/src-tauri/` | `cd desktop/src-tauri && cargo check` | 零警告 + **须实机验收** |

必须用隔离环境的 `.venv\Scripts\python.exe`，不用系统 Python（系统环境没装 sherpa-onnx / soundfile）。

## 当前阶段

Phase 3（原型与决策）已完成，Phase 4（生产实现）**未开始**。7 项发布阻塞项见 `docs/project-overview.md` 第 6 节。

**未实现，不要假设存在**：流式 ASR（识别文本目前靠外部注入）、TTS 播放队列与真实打断、Canvas 2D 生产渲染器、超时/重连/错误恢复、系统托盘、`wake.rejected` 事件产出点。

## 注意事项

- **本目录不是 git 仓库**，没有回滚能力。改动前确认工作区状态，大改动前先备份目标文件。
- 文档要同步更新：实测数据进 `docs/research/prototype-results.md`，新例程进 `docs/routines.md`，依赖与模型版本进 `THIRD_PARTY_NOTICES.md`。
- 控制台中文乱码是 Windows 代码页显示问题，UTF-8 字节正确，**不是缺陷，不要去「修」**。
- `models/` 约 413 MB，其中 `kws.tar.bz2` + `tts.tar.bz2` 共 192 MB 是可删归档。不要把模型文件当代码改动处理。
- 桥接安全姿态已加固（bearer token 强制、loopback 校验、URL 凭据拦截、turn_id 编码），改 `core/session_bridge.py` 时不得降级这些校验。
