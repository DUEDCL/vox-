# Vox

Windows 平台上的 EvoX 本地语音唤醒对话原型。

> 当前阶段：Phase 3（原型与技术决策）已完成；Phase 4（生产实现）尚未开始。项目尚未达到发布标准。

## 项目目标

```text
中文唤醒 → 本地语音识别 → EvoX 会话 → 本地 TTS 回复 → 连续对话 → 随时打断
```

核心原则：

- **本地优先**：唤醒、VAD（语音活动检测）与 TTS（语音合成）在本机执行；项目代码不保存、不上传音频。
- **组件可替换**：KWS（关键词唤醒）、ASR（语音识别）、TTS、会话传输与 UI（用户界面）均位于版本化契约之后。
- **证据分级**：严格区分 AUTO（自动测试）、SIM（模拟验证）、REAL-MIC（真实麦克风）、REAL-EVOX（真实会话）和 REAL-WIN（真实窗口）结果。

## 当前实现

- `core/state.py`：严格语音状态机（idle/listening/thinking/speaking/cancelled/error）。
- `core/providers.py`：sherpa-onnx KWS、Silero VAD、sounddevice 麦克风采集及可选 VoxCord 适配器。
- `core/session_bridge.py`：带 Bearer Token（承载令牌）认证的 EvoX localhost HTTP 桥接。
- `vox_plugin/plugin.py`：EvoX 插件门面，包含 start/stop/pause/resume/status/devices/diagnose/wake_test/cancel 等工具。
- `contracts/voice-events.schema.json`：版本 `1` 的语音事件契约。
- `desktop/`：Tauri 2 + TypeScript + Vite 的透明置顶唤醒窗口原型。

## 技术选型

| 层 | 主方案 | 备选/降级 |
|---|---|---|
| 语音运行时 | sherpa-onnx 1.13.4 | — |
| 中文唤醒 | Zipformer KWS | openWakeWord |
| VAD | Silero VAD | — |
| TTS | MeloTTS VITS | Kokoro-82M |
| EvoX 桥接 | `LocalEvoXTransport` | 其它 `ConversationTransport` 实现 |
| UI 渲染 | Canvas 2D + CSS（v1） | 静态 CSS；WebGL（v2） |
| 桌面外壳 | Tauri 2 | — |

完整选型依据见 [`docs/adr/001-voice-stack-selection.md`](docs/adr/001-voice-stack-selection.md)。

## 快速验证

### Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 自动测试与模拟链路

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/smoke_voice.py
.\.venv\Scripts\python.exe scripts/e2e_simulated.py
.\.venv\Scripts\python.exe tmp_proto/t10_voice_stack_validation.py
```

当前已记录基线：`600 passed, 3 skipped`（2026-08-16，干净 shell 下复现两次）。3 个 skipped（跳过）用例：2 个依赖本机不存在的可选 VoxCord checkout（检出目录），1 个需要创建符号链接的权限（本账户没有）。记基线前先确认 `env | grep PYTHON` 为空 —— 设了 `PYTHONUTF8` 会改变结果。

### 桌面构建

```powershell
Push-Location desktop
npm run build
Pop-Location

Push-Location desktop/src-tauri
cargo check
Pop-Location
```

## 当前验证状态

已验证：

- sherpa-onnx KWS/VAD 模型在当前 Windows 环境加载成功。
- 合成的 `你好问问` 可触发 KWS；12 秒静音流零误触。
- 一次真实麦克风口述 `你好问问` 唤醒成功。
- 模拟回合覆盖唤醒、识别文本、桥接、回复、TTS 事件、连续对话、取消与停止。
- CSS、Canvas 2D、WebGL 三条渲染路线原型均可运行。

尚未验证（发布阻塞项）：

- 多场景真实麦克风唤醒质量与 Silero 端点验收。
- 真实 EvoX 会话桥接、流式回复、取消、超时和重连。
- 真实 WebView2（网页视图）透明合成、DPI、多显示器、托盘及 RDP（远程桌面）降级。
- ≥30 分钟 CPU、内存和 FPS 持续运行画像。

## 目录结构

```text
contracts/       版本化事件契约
core/            状态机、语音提供器、会话桥接
desktop/         Tauri/TypeScript 桌面窗口
docs/            项目、架构、需求、测试、ADR 与调研文档
vox_plugin/     EvoX 插件门面
models/          本地 KWS/VAD/TTS 模型及归档
scripts/         冒烟与模拟端到端脚本
tests/           Python 自动化测试
tmp_proto/       原型验证脚本与 UI 技术验证页
```

## 文档入口

- **[交接文档（接手先读这份）](docs/handoff.md)**
- [项目总览与当前进度](docs/project-overview.md)
- [技术架构与组件边界](docs/architecture.md)
- [需求文档](docs/requirements.md)
- [测试文档](docs/testing.md)
- [可重复编码例程](docs/routines.md)
- [语音栈选型 ADR](docs/adr/001-voice-stack-selection.md)
- [原型实测结果](docs/research/prototype-results.md)
- [第三方组件与许可证](THIRD_PARTY_NOTICES.md)

## 重要说明

- 已是 Git 仓库（基线 `9f7d923`，平台化进展见 `git log`）。
- `models/` 中包含较大的模型目录和下载归档，发布前需要确定模型分发及归档清理策略。
- 本项目是 EvoX 生态中的原型项目，真实 EvoX 会话端点尚未完成联调。
