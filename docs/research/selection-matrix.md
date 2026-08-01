# Voice Stack Selection Matrix

> Second pass, 2026-07-22, after live registry verification (see `open-source-landscape.md`). Scores are 0–5 per dimension; totals are weighted. "UI" is split into five evidence sub-scores per the merged plan, averaged into the 10% UI weight.

## Weights (unchanged from original plan)

| 维度 | 权重 |
|---|---:|
| EvoX 原生兼容性 | 20% |
| Windows 系统级常驻能力 | 15% |
| 中文本地唤醒质量 | 15% |
| 实时语音延迟与打断 | 10% |
| 液态玻璃 UI 可实现性 | 10% |
| 可替换 STT/LLM/TTS | 10% |
| 维护状态与测试质量 | 8% |
| 许可证与可分发性 | 7% |
| 隐私与本地优先 | 5% |

UI sub-evidence (each 0–5, averaged): transparent-window compatibility · visual quality · realtime audio response · performance/degradation · license.

## Voice core / assistant candidates

| Candidate | EvoX 20 | Windows 15 | Zh wake 15 | Realtime 10 | UI 10 | Replaceable 10 | Maint 8 | License 7 | Privacy 5 | Total /5 | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| VoxCord core (adjacent repo) | 3 | 4 | 4 | 3 | 3 | 5 | 4 | 4 | 4 | 3.70 | **核心底座（原型参考）** — user confirmed own project; selective reuse permitted (2026-07-26) |
| sherpa-onnx runtime + own bridge | 2 | 5 | 4 | 4 | 1 | 5 | 5 | 4 | 5 | 3.70 | **核心底座（发布路径）** — v1.13.4 verified, single runtime |
| silero-vad | 1 | 5 | 0 | 5 | 0 | 5 | 5 | 5 | 5 | (module) | **单一模块复用** (VAD/barge-in) |
| faster-whisper | 1 | 4 | 0 | 2 | 0 | 5 | 4 | 5 | 5 | (module) | **单一模块复用** (fallback STT) |
| SenseVoiceSmall via sherpa-onnx | 1 | 4 | 0 | 5 | 0 | 4 | 4 | 3 | 5 | (module) | **单一模块复用** pending weight-license capture |
| openWakeWord | 1 | 4 | 2 | 4 | 0 | 4 | 3 | 4 | 5 | 2.80 | 备选唤醒，仅当 sherpa KWS 实测不达标 |
| OpenVoiceOS | 2 | 1 | 3 | 3 | 1 | 4 | 4 | 4 | 4 | 2.45 | **排除**（无真实 Windows 支持） |
| Wyoming satellite (binary) | 1 | 1 | 3 | 4 | 0 | 4 | 3 | 5 | 5 | 2.30 | **仅参考**（协议设计）；二进制排除 |
| Rhasspy 2.5 | 1 | 1 | 2 | 2 | 0 | 3 | 1 | 5 | 4 | 1.85 | **排除**（2021 冻结） |
| Pipecat v1.6.0 | 2 | 4 | 2 | 5 | 1 | 5 | 5 | 5 | 2 | 3.15 | **仅参考/观察** — 双编排层成本 > 收益 |
| LiveKit Agents | 1 | 1 | 2 | 5 | 0 | 4 | 5 | 4 | 2 | 2.20 | **排除**（SFU 依赖 + Windows 弱） |
| Piper / piper1-gpl | 1 | 4 | 0 | 4 | 0 | 4 | 2 | 1 | 5 | 2.35 | **排除**（GPL-3.0 + 中文最弱 + 上游归档） |
| Kokoro-82M (direct) | 1 | 4 | 0 | 4 | 0 | 4 | 2 | 4 | 5 | 2.85 | 备选，经 sherpa-onnx 封装而非独立引入 |
| EvoX native voice plugin | — | — | — | — | — | — | — | — | — | n/a | **不存在可验证资产**（见 evox-community.md） |
| EvoX `evox-sessions` MCP | 5 | 5 | 0 | 3 | 0 | 3 | 4 | 3 | 5 | (module) | **单一模块复用** — 会话桥接传输层 |

## UI technology candidates (UI five-evidence split)

| UI candidate | Transparent | Visual | Audio-driven | Degradation | License | Avg | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Self-built Canvas 2D + CSS glass layers (v1) | 4 | 4 | 5 | 5 | 5 | 4.6 | **直接采用** — no deps, deterministic, testable |
| Static CSS glass (degradation mode) | 5 | 2 | 2 | 5 | 5 | 3.8 | **直接采用**（降级档） |
| paper-design/shaders (WebGL, Apache-2.0) | 3 | 5 | 4 | 3 | 5 | 4.0 | **核心备选** — v2 upgrade path if Canvas 2D 视觉不达标 |
| Three.js/OGL/drei transmission | 3 | 5 | 4 | 2 | 5 | 3.8 | **仅参考**（bundle + GPU 风险） |
| SVG gooey/feTurbulence filters | 3 | 3 | 3 | 4 | 5 | 3.6 | **单一模块复用**（边缘黏性，元素级；不可用于背景采样） |
| WebGL-Fluid-Simulation advection | 3 | 4 | 3 | 2 | 5 | 3.4 | **仅参考**（算法） |
| siriwave formula | 4 | 3 | 5 | 5 | 5 | 4.4 | **单一模块复用**（波形数学，内联） |
| ShaderToy/CodePen liquid-glass demos | 2 | 5 | 3 | 2 | 1 | 2.6 | **排除**（许可） |
| window-vibrancy Acrylic | 2 | 4 | 0 | 3 | 5 | 2.8 | **仅参考**（与 transparent 冲突需重测） |
| Voice Satellite Card (Siri skins) | 1 | 4 | 4 | 3 | 5 | 3.4 | **仅参考**（状态映射与 RMS 模式） |

## Decision summary

- **可直接采用**: 自建 Canvas 2D + CSS 玻璃渲染（v1 主路径）、静态 CSS 降级。
- **核心底座**: sherpa-onnx 运行时（发布路径）+ VoxCord 架构与测试资产（原型参考；用户已确认其为自有项目，允许选择性复用）。
- **单一模块复用**: silero-vad（端点/打断）、faster-whisper（fallback STT）、SenseVoiceSmall（快速 ASR，先取证许可证）、`evox-sessions` MCP（会话桥）、siriwave 波形数学、SVG gooey（元素级）。
- **仅参考**: Wyoming 协议、Pipecat、OVOS solver 设计、Voice Satellite 状态映射、window-vibrancy、Three.js 栈、paper-design/shaders（同时是 v2 备选）。
- **排除**: piper1-gpl（GPL-3.0）、Rhasspy、LiveKit Agents、OpenVoiceOS（Windows）、audiowaveform（GPL-3.0）、ShaderToy/CodePen 代码。

## Next gate

Isolated prototypes (plan step t10): sherpa-onnx KWS+ASR+TTS in `.venv` on this Windows host; Canvas 2D vs static-CSS orb in the Tauri transparent window. Scores marked with license/maintenance uncertainty (VoxCord license, SenseVoice weights) stay release blockers until prototype evidence lands in `prototype-results.md`.
