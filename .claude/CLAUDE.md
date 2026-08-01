# EvoX Voice Wake — 项目规则

全局准则见 `~/.claude/CLAUDE.md`。本文件只写本项目特有的约束。

## 三条设计红线(不得违反)

1. **本地优先** — 唤醒、VAD（语音活动检测）、TTS（语音合成）、声纹校验全部本机执行；项目代码不保存、不上传音频。新增依赖若含云调用或 telemetry（遥测），直接否决。声纹环形缓冲永不落盘；记忆库只存文本、永不存音频。
2. **组件可替换** — KWS/ASR/TTS/会话传输/**agent 后端**都在契约之后。任何 `sherpa-onnx`、`sounddevice`、VoxCord 的类型都不得出现在 `contracts/voice-events.schema.json` 或公开事件结构里（`additionalProperties: false` 是第一道闸门）。`AgentDescriptor`/`Task`/`AgentChunk` 的字段只许 `str`/`int`/`float`/`frozenset`/`tuple`/`Mapping`。
3. **验证等级诚实** — 七级证据 DOC < AUTO < SIM < REAL-MIC < **REAL-AGENT** < REAL-EVOX < REAL-WIN。禁止把低等级当高等级用，禁止把 mock（模拟）验证写成真机验收。**mock 子进程只算 SIM，不算 REAL-AGENT**。声明结论必须标注等级。

## 改完什么跑什么

完整例程见 `docs/routines.md`。最小回归对照表：

| 改动范围 | 命令 | 期望 |
|---|---|---|
| `core/` `evox_plugin/` | `.venv\Scripts\python.exe -m pytest tests -q` | **43 passed, 2 skipped** |
| `contracts/` 或事件结构 | `pytest tests/test_event_schema.py tests/test_events.py tests/test_voice_contract.py tests/test_plugin_tools.py -q` | 全绿 |
| `core/events.py` | `pytest tests/test_events.py -q` | 8 passed |
| `core/audio/`(除 speaker) | `pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q` | 全绿 |
| `core/audio/speaker.py` | `pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | 全绿（**不需要声纹模型**） |
| `core/tools/` | `pytest tests/test_tools.py tests/test_tool_security.py -q` | 全绿（P4 起） |
| `core/memory/` | `pytest tests/test_memory.py -q` | 全绿（P3 起） |
| `core/agents/` | `pytest tests/test_agent_contract.py tests/test_agent_cli.py -q` | 全绿（P5 起） |
| `core/dispatch/` | `pytest tests/test_router.py tests/test_dispatcher.py -q` | 全绿（P6 起） |
| `core/session_bridge.py` | `pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q` | 全绿 |
| `desktop/src/` | `cd desktop && npm run build` | tsc + vite 通过 |
| `desktop/src-tauri/` | `cd desktop/src-tauri && cargo check` | 零警告 + **须实机验收** |

必须用隔离环境的 `.venv\Scripts\python.exe`，不用系统 Python（系统环境没装 sherpa-onnx / soundfile）。

skip 数会随环境变化（VoxCord、模型是否存在），**passed 数下降才是回归**。

## 当前阶段

Phase 4（生产实现）**进行中**：P0 骨架已落地，下一步 P1（声纹门）。十阶段划分与 10 项发布阻塞项见 `docs/project-overview.md` 第 5、6 节。

决策记录：ADR 001 语音栈 · ADR 002 声纹准入 · ADR 003 agent 接入 · ADR 004 记忆 · ADR 005 派发与工具门。

**未实现，不要假设存在**：
- 声纹门接线（provider 已完成，环形缓冲与 `require_verification` 未接；37 MB 模型未下载）
- `wake.rejected` 产出点；`feed()` 的真实 score（当前硬编码 `1.0`）
- 平台层四包的**任何实现** —— `core/{agents,dispatch,tools,memory}/` 现在**只有 `contract.py`**
- 流式 ASR（识别文本靠外部注入）、TTS 播放队列与真实打断
- 唤醒球运行时显隐（可见性现由 `EVOX_WAKE_VISIBLE` 静态决定）、Canvas 2D 生产渲染器、工具确认 UI
- 超时/重连/错误恢复、系统托盘

## 注意事项

- **已是 git 仓库**（基线 `9f7d923`）。改动前 `git status --short` 确认工作区，不要覆盖无关脏文件。破坏性 git 操作（`reset --hard`、`push --force`、`clean -f`）一律先问。
- **`VoiceState` 六态不改**，`contracts/voice-events.schema.json` **字节不变**、version 保持 `"1"`。平台事件走新增的 `contracts/agent-events.schema.json`。
- **信封只在 `core/events.py` 构造**。新事件类型加进契约文件即可，Python 侧不需要同步改。
- **`enrollment/` 是生物特征**，已在 `.gitignore` 内，永不提交；查看注册状态只用 `describe()`，绝不输出向量。
- **声纹 fail-closed 断言不许绕过**：模型缺失、无人注册、校验抛异常都必须落在拒绝一侧。一个静默放行的声纹门比没有门更糟。
- **`shell.run` 默认关闭**，白名单外的命令**拒绝而非询问**（询问会训练出无脑点确认的习惯）。
- 文档要同步更新：实测数据进 `docs/research/prototype-results.md`，新例程进 `docs/routines.md`，依赖与模型版本进 `THIRD_PARTY_NOTICES.md`。
- 控制台中文乱码是 Windows 代码页显示问题，UTF-8 字节正确，**不是缺陷，不要去「修」**。
- `models/` 约 413 MB，其中 `kws.tar.bz2` + `tts.tar.bz2` 共 192 MB 是可删归档，另需声纹模型 37 MB。不要把模型文件当代码改动处理。
- 桥接安全姿态已加固（bearer token 强制、loopback 校验、URL 凭据拦截、turn_id 编码），改 `core/session_bridge.py` 时不得降级这些校验；它被包装成 `agents/evox.py` 后同样不得降级。
- `github.com` / `api.github.com` / `raw.githubusercontent.com` 的 WebFetch 在本环境被拦截，无法读一手 README。开源项目判定只能标「社区来源」，不得当官方确认用。
