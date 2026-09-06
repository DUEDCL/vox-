# Vox

Windows 平台上的语音唤醒对话平台：唤醒词与声纹准入在本机完成，语音识别、合成和复杂任务可按配置使用本机或云端能力。

> **当前状态（2026-09-06）**：Phase 4（平台化生产实现）进行中。核心代码、控制台、语音入口和桌面唤醒球已经接通；发布前仍缺少完整的 REAL-MIC、REAL-AGENT、REAL-EVOX 和 REAL-WIN 验收。测试与真机证据请以仓库中的实际命令输出和 `docs/` 记录为准。

## 这是什么

```text
唤醒词（本机 KWS）
  → 声纹确认（本机）
  → 语音输入
  → 本地工具，或可替换的 agent 后端
  → 流式回复
  → TTS 播放
  → 连续对话 / 唤醒词打断
```

主要能力：

- **本机安全边界**：待机唤醒词音频、VAD、声纹注册与校验在本机完成；声纹数据不进版本库。
- **可配置的语音链路**：KWS、VAD、ASR、TTS、agent 和桌面 UI 都通过配置或适配器替换。
- **本地工具与记忆**：文件读取、搜索、终端、MCP 和 SQLite/FTS5 记忆均有独立安全门。
- **本机控制台**：查看就绪状态、补齐模型和声纹、运行模型测试、管理配置与事件流。
- **Windows 桌面唤醒球**：Tauri 2 外壳、透明置顶窗口、系统托盘与 Python 事件通道。

## 隐私与数据流

Vox 的隐私边界不是“所有音频永不出网”，而是明确区分待机准入链路与被接受请求的处理链路：

- 待机阶段的 KWS、VAD 和声纹校验在本机运行，唤醒词和声纹不会上传。
- 通过唤醒和声纹准入后，**被接受的那一句语音**可以按 `config/voice.toml` 的配置发送到云端 ASR；也可以切换到本机 ASR。
- TTS 同样支持云端与本机提供器，具体以 `config/voice.toml` 为准。
- 音频不写入记忆库；`enrollment/`、`models/`、`.env` 和 `memory/` 默认不进 Git。
- 云端凭据只从环境变量或本机 `.env` 读取，配置文件只保存变量名，不保存密钥值。

如果你的部署要求全程离线，请检查 `[asr]`、`[tts]` 的 `provider`，并运行 `run_voice.py --check` 验证模型和依赖是否齐全。

## 快速开始

### 1. 创建 Python 环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

只需要语音运行时，也可以安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
```

模型权重不随 Git 仓库分发，放在 `models/` 或通过 `VOX_KWS_MODEL_DIR`、`VOX_ASR_MODEL_DIR`、`VOX_TTS_MODEL_DIR`、`VOX_VAD_MODEL` 指定。获取与体积说明见 [模型分发策略](docs/model-distribution.md)。

### 2. 启动本机控制台

```powershell
.\.venv\Scripts\python.exe scripts/run_console.py
```

控制台绑定回环地址并生成访问 token。打开后先看“就绪清单”：它会告诉你缺少哪些模型、依赖或声纹档案；声纹注册也可以在控制台完成。

### 3. 启动语音或桌面入口

```powershell
.\.venv\Scripts\python.exe scripts/run_voice.py --check    # 只检查就绪状态
.\.venv\Scripts\python.exe scripts/run_voice.py            # 唤醒 → 识别 → 派发 → 播放
.\.venv\Scripts\python.exe scripts/run_desktop.py          # 不开麦克风的文字对话入口
```

日常使用也可以运行 `scripts/vox.cmd`，或用下面的脚本重建桌面快捷方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/make_desktop_shortcut.ps1
```

## 开发与验证

### Python 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts/smoke_voice.py
.\.venv\Scripts\python.exe scripts/e2e_simulated.py
```

不要把 README 里的固定通过数当作永久基线；测试数量会随环境和可选依赖变化，发布前应以当前命令输出为准。

### 桌面构建

```powershell
Push-Location desktop
npm install
npm run build
Pop-Location

Push-Location desktop/src-tauri
cargo check
Pop-Location
```

### 真机验收

以下命令需要麦克风、扬声器、模型或外部服务，不能用单元测试结果代替：

```powershell
.\.venv\Scripts\python.exe scripts/acceptance/live_wake.py
.\.venv\Scripts\python.exe scripts/acceptance/live_conversation.py
.\.venv\Scripts\python.exe scripts/acceptance/probe_agents.py
.\.venv\Scripts\python.exe scripts/acceptance/resource_profile.py --minutes 30
```

## 技术组成

| 层 | 当前实现 | 备注 |
|---|---|---|
| 唤醒 | sherpa-onnx Zipformer KWS | 本机运行 |
| VAD | Silero VAD | 本机运行 |
| ASR | 云端 provider 或 sherpa-onnx 本机 provider | 默认与实际选择以 `config/voice.toml` 为准 |
| 声纹 | 3D-Speaker CAM++（经 sherpa-onnx） | 本机运行，`enrollment/` 不入库 |
| TTS | 云端 provider 或本机 VITS | 默认与实际选择以 `config/voice.toml` 为准 |
| agent | CLI、ACP、OpenAI 兼容 HTTP、EvoX | 见 `config/agents.toml` |
| 工具 | `fs.read`、`web.search`、`shell.run`、MCP | 默认安全门与确认策略见配置和 ADR |
| 记忆 | SQLite + FTS5 + Markdown 事实层 | 不保存音频 |
| 控制台 | Python 标准库 `http.server` + 单文件前端 | 仅回环监听 |
| 桌面 | Tauri 2 + TypeScript | 透明置顶唤醒球 |

## 目录结构

```text
contracts/       版本化事件契约与 agent/MCP 配置形状
config/          语音、声纹、工具、agent、记忆和 MCP 配置
core/            状态机、语音、控制台、工具、记忆、agent、派发与桥接
desktop/         Tauri/TypeScript 桌面唤醒球
docs/            架构、需求、测试、ADR、交接与研究记录
models/          本地模型（gitignore）
vox_plugin/      VoicePlugin、VoiceRuntime 与语音栈组装
scripts/         启动、构建和真机验收入口
tests/           Python 自动化测试
```

## 文档入口

- [项目交接](docs/handoff.md)：接手项目先读。
- [项目总览与当前进度](docs/project-overview.md)
- [技术架构与组件边界](docs/architecture.md)
- [需求文档](docs/requirements.md)
- [测试文档](docs/testing.md)
- [可重复编码例程](docs/routines.md)
- [模型分发策略](docs/model-distribution.md)
- [Git 工作流](docs/git-workflow.md)
- [Backlog](docs/backlog.md)
- [仓库文档分层与 `CLAUDE.md` 说明](docs/repository-guide.md)
- [ADR 索引](docs/adr/)
- [第三方组件与许可证](THIRD_PARTY_NOTICES.md)

## 运行安全边界

- 控制台只允许回环访问，每个请求校验 token。
- `shell.run`、MCP 和远端 agent 的能力由配置中的安全门、白名单和确认策略限制。
- 网页控制台不能直接修改关键安全边界；需要修改时应编辑版本化配置并重新启动。
- 不要把 `.env`、声纹、记忆、模型权重或本机截图当成通用项目资产提交。

## 当前限制

这个仓库目前更接近“可运行的工程原型 / 生产化进行中”，而不是已经打包发布的桌面产品。完整真机验收、真实 agent 联调、真实 EvoX 桥接、透明窗口兼容性和持续资源画像仍在 backlog 中；请不要把 AUTO/SIM 测试描述成 REAL 证据。
