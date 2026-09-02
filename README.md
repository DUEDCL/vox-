# Vox

Windows 平台上的 Vox 本地优先语音唤醒对话平台。

> 当前阶段：Phase 4（平台化生产实现）进行中。P0–P8 已落地，**本机 web 控制台已可用**；
> 尚未达到发布标准 —— REAL 级证据（真机麦克风 / 真实 agent / 真实透明窗口）仍缺。

## 项目目标

```text
说出唤醒词 → 声纹确认是本人 → 唤醒球弹出 → 本地识别语音
    ├─ 简单的事：平台自己做（读文件 / 搜索 / 终端 / 记忆 / MCP 工具），毫秒级
    └─ 复杂的事：派发给 claude / codex / opencode / evox，路由汇总
→ 流式回复 → TTS 朗读 → 继续多轮对话 → 说唤醒词随时打断
```

核心原则：

- **本地优先**：唤醒、VAD（语音活动检测）、ASR（语音识别）、TTS（语音合成）与声纹校验全部在本机执行；项目代码不保存、不上传音频。
- **组件可替换**：KWS（关键词唤醒）、ASR、TTS、会话传输、agent 后端与 UI 均位于版本化契约之后。
- **证据分级**：严格区分 DOC < AUTO < SIM < REAL-MIC < REAL-AGENT < REAL-EVOX < REAL-WIN，**mock 不算真机**。

## 当前实现

一条命令起来，浏览器里看得见缺什么、点得动补什么：

```powershell
.\.venv\Scripts\python.exe scripts/run_console.py
```

- `core/state.py`：严格语音状态机（idle/listening/thinking/speaking/cancelled/error）。
- `core/audio/`：sherpa-onnx KWS/VAD/ASR/TTS、声纹准入、sounddevice 麦克风采集（两段模式：唤醒 → 聆听）、`config.py` 语音栈配置。
- `core/console/`：**本机回环 web 控制台** —— 就绪清单、浏览器录声纹、模型测试、配置编辑、agent 与 MCP 管理、人物档案、事件流。零新依赖（标准库 `http.server`），见 [ADR 006](docs/adr/006-local-console.md)。
- `core/tools/`：一道政策门 + `fs.read` / `web.search` / `shell.run` / **MCP 远端工具**（[ADR 007](docs/adr/007-mcp-tools.md)）。`shell.run` 默认关，MCP 三层默认关。
- `core/agents/`：`cli` / `acp` / `http` / `evox` 四种适配器，配置在 `config/agents.toml`（**没有放密钥的键**）。
- `core/dispatch/`：意图识别、五维路由、三模式汇总、熔断器。
- `core/memory/`：SQLite + FTS5 单文件 + Markdown 事实层，中文双字索引，凭据整条拒绝。
- `core/session_bridge.py`：带 Bearer Token 认证的 EvoX localhost HTTP 桥接。
- `vox_plugin/`：`plugin.py` 门面 + `runtime.py` 装配 + `voice_stack.py` 语音栈组装。
- `contracts/`：语音事件 9 种（**字节不变**，SHA-256 钉死）+ 平台事件 12 种 + agent 与 MCP 配置形状。
- `desktop/`：Tauri 2 透明置顶唤醒球（AE 预渲染雪碧图序列为主路径，手写 Canvas 2D 为退路），选择性穿透 + 系统托盘（七项）+ Python↔前端事件通道。

## 技术选型

| 层 | 主方案 | 备选/降级 |
|---|---|---|
| 语音运行时 | sherpa-onnx 1.13.4 | — |
| 中文唤醒 | Zipformer KWS | openWakeWord |
| VAD | Silero VAD | — |
| ASR | streaming Zipformer zh-14M | faster-whisper / SenseVoiceSmall |
| TTS | MeloTTS VITS | Kokoro-82M |
| 声纹准入 | 3D-Speaker CAM++（经 sherpa-onnx，零新依赖；dim 192） | 同系列 ERes2Net（2026-08-29 起改为备选） |
| agent 接入 | headless CLI 子进程 + ACP + OpenAI 兼容 HTTP | EvoX 桥接 |
| 工具扩展 | MCP over stdio（三层默认关） | — |
| 记忆 | SQLite + FTS5 单文件 + Markdown | 明确不做向量检索 |
| 搜索后端 | 自建 SearxNG（回环）→ DuckDuckGo 无 key 兜底，**都默认关** | — |
| 控制台 | 标准库 `http.server` + 单文件前端（零新依赖） | — |
| UI 渲染 | AE 预渲染序列（`sequence.ts`）+ Canvas 2D 退路 + CSS | 静态帧；WebGL（v2） |
| 桌面外壳 | Tauri 2 | — |

完整选型依据见 [`docs/adr/001-voice-stack-selection.md`](docs/adr/001-voice-stack-selection.md)。

## 快速开始

### 1. Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 2. 起控制台，按它说的补齐

```powershell
.\.venv\Scripts\python.exe scripts/run_console.py
```

浏览器会打开一个带 token 的回环地址。**就绪清单**会逐项说明还缺什么（模型、声纹注册）
以及怎么补；声纹可以直接在页面上录 3 段完成注册，音频只在内存、永不落盘。

### 3. 说话

```powershell
.\.venv\Scripts\python.exe scripts/run_voice.py            # 全链路：唤醒 → 识别 → 派发 → 出声
.\.venv\Scripts\python.exe scripts/run_voice.py --check    # 只打印就绪清单
.\.venv\Scripts\python.exe scripts/run_desktop.py          # 打字对话（不开麦克风）
```

### 自动测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/smoke_voice.py
.\.venv\Scripts\python.exe scripts/e2e_simulated.py
```

当前可复现基线：**`1009 passed, 3 skipped`**（2026-08-28，干净 shell、仓库 `.venv`、
清空代理变量、仓库隔离的 `--basetemp`；全量 AUTO）。3 个 skipped 的构成：2 个依赖本机
不可用的可选 VoxCord（目录在但依赖未装，见 `docs/backlog.md` B1）、1 个需要创建符号链接
的权限。**skip 数会随环境变化，passed 数下降才是回归**。记基线前先确认没有设置
`PYTHONUTF8` / `PYTHONIOENCODING`。

### 真机验收（需要人在场）

```powershell
.\.venv\Scripts\python.exe scripts/acceptance/live_wake.py           # 唤醒率
.\.venv\Scripts\python.exe scripts/acceptance/live_conversation.py   # 全链路
.\.venv\Scripts\python.exe scripts/acceptance/probe_agents.py        # REAL-AGENT 探测
.\.venv\Scripts\python.exe scripts/acceptance/resource_profile.py --minutes 30
```

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

已验证（AUTO / SIM）：

- sherpa-onnx KWS / VAD / ASR / TTS 模型在本机 Windows 环境加载并推理成功。
- 合成的 `你好问问` 可触发 KWS；12 秒静音流零误触。
- 一次真实麦克风口述 `你好问问` 唤醒成功（2026-07-26，单次）。
- 声纹判别力：真实人声簇内 0.736 / 簇间 0.370，阈值 0.5 落在间隙；校验耗时 41 ms。
- 流式 ASR 用真实模型转写 bundled wav 成功；TTS 真实模型合成实测（控制台「模型测试」可复现）。
- 记忆跨进程持久性（双进程自动化实测）+ 多线程并发（真线程，7 例）。
- 派发 / 路由 / 汇总 159 例、工具安全 89 条拒绝矩阵、MCP 门 51 例、控制台 82 例。
- 控制台页面渲染与 API 交互（SIM：快照 + 截图 + 真实 HTTP）。

尚未验证（发布阻塞项，需要人在场或外部服务）：

- 多场景真实麦克风唤醒质量与 Silero 端点验收（REAL-MIC）。
- 声纹实测：本人通过率 / 他人拒绝 / 录音回放边界（REAL-MIC）。
- 真实外部 agent 跑通一轮（REAL-AGENT）—— 三个后端 2026-08-24 全部**试过且被挡**
  （未登录 / 挂起 / 连不上云端点），`probe_agents.py` 是恢复后的重试入口。
- 真实 EvoX 会话桥接与流式首字延迟（REAL-EVOX）。
- 真实 WebView2 透明合成、DPI 125–175%、多显示器、托盘、RDP 降级（REAL-WIN）。
- ≥30 分钟 CPU / 内存 / FPS 持续运行画像（REAL-WIN，`resource_profile.py` 可无人值守启动）。
- 第三方 MCP server 完成一次真实调用（当前仅 SIM）。

## 目录结构

```text
contracts/       版本化事件契约 + agent / MCP 配置形状
core/            状态机、语音提供器、控制台、工具、记忆、agent、派发、桥接
config/          voice / speaker / tools / agents / memory / mcp 六份配置（**无密钥键**）
desktop/         Tauri/TypeScript 唤醒球
docs/            项目、架构、需求、测试、ADR 与调研文档
vox_plugin/      插件门面、VoiceRuntime、语音栈组装
models/          本地模型（gitignore，获取与分发策略见 docs/model-distribution.md）
scripts/         入口（run_console / run_voice / run_desktop）+ acceptance/ 真机验收
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
- [Git 工作流（一条主干，一次会话一个提交）](docs/git-workflow.md)
- [模型分发策略](docs/model-distribution.md)
- [Backlog（识别但故意没做的技术债）](docs/backlog.md)
- ADR：[001 语音栈](docs/adr/001-voice-stack-selection.md) · [002 声纹准入](docs/adr/002-speaker-verification.md) · [003 agent 接入](docs/adr/003-agent-integration-protocol.md) · [004 记忆](docs/adr/004-memory-architecture.md) · [005 派发与工具门](docs/adr/005-task-dispatch-model.md) · [006 本地控制台](docs/adr/006-local-console.md) · [007 MCP 工具](docs/adr/007-mcp-tools.md)
- [原型实测结果](docs/research/prototype-results.md)
- [第三方组件与许可证](THIRD_PARTY_NOTICES.md)

## 重要说明

- **控制台开了一个监听端口**，姿态是：强制回环绑定（`0.0.0.0` 拒绝构造）、每请求校验
  随机 token（含页面本身）、token 不进日志不进 `describe()`。安全边界（`shell.enabled`、
  `fs.roots`、`speaker.require_verification`、agent 的 `command`/`url`、MCP 的
  `require_confirmation`/`allow`）**不可从网页修改** —— 关掉一道防线应该是一次打开
  编辑器的动作。细则见 [ADR 006](docs/adr/006-local-console.md) 第 3 节。
- **确认面只有一个**：`shell.run` 与 MCP 工具的确认都在唤醒球上，控制台只报「有一个
  待确认」，连命令原文都不显示。
- `models/` 约 597 MB（含 261 MB 可删归档），发布分发策略见
  [docs/model-distribution.md](docs/model-distribution.md)。
- 本项目是 EvoX 生态中的项目，但 EvoX 已降级为众多可替换 agent 后端之一；真实 EvoX
  会话端点尚未完成联调。
