# 可重复编码例程

> 工作区：`D:\program\vioce-wake`。下面的命令按 Windows PowerShell 编写；Git Bash 可将 `\.venv\Scripts\python.exe` 改为 `./.venv/Scripts/python.exe`。

## 隔离语音环境设置

适用时机：首次设置工作区、删除 `.venv` 后恢复，或固定依赖版本发生变化时。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

运行时依赖固定在 `requirements-voice.txt`;开发与测试环境使用 `requirements-dev.txt`,它会递归安装运行时依赖并固定 pytest。模型文件不由 pip 安装,仍需按“模型下载与完整性检查”例程单独准备。

## 日常修改后的最小回归

适用时机：修改 `core/`、`evox_plugin/`、`contracts/` 或 Python 测试后。

```powershell
python -m pytest tests -q
python scripts/smoke_voice.py
python scripts/e2e_simulated.py
```

第一条检查单元、契约和适配器；第二条检查插件最小生命周期；第三条覆盖模拟链路：唤醒、识别文本、会话发送、回复、TTS 事件、连续对话、取消和停止。

## 契约或事件字段变更

适用时机：修改 `contracts/voice-events.schema.json`、事件类型、状态机或插件事件 payload 后。

```powershell
python -m pytest tests/test_event_schema.py tests/test_voice_contract.py tests/test_plugin_tools.py -q
```

同时检查 `contracts/voice-events.schema.json` 的枚举是否覆盖新增事件。事件版本变更时，更新协议文档和兼容性测试，不要只改生产代码。

## Provider 适配器变更

适用时机：修改 `core/providers.py`、VoxCord 动态导入、Sherpa 模型加载或设备发现后。

```powershell
python -m pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q
python -c "from core.providers import VoxCordAdapter; print(VoxCordAdapter().load())"
```

没有 VoxCord、Sherpa 或音频驱动时，导入仍应成功；不可用状态应返回原因，而不是在模块导入阶段崩溃。

## Sherpa 中文 KWS 验证

适用时机：更换 Sherpa 版本、ONNX 模型、关键词文件、阈值、线程数或音频分块大小后。该例程不打开麦克风，只使用随模型附带的 wav。

```powershell
.\.venv\Scripts\python.exe tmp_proto/test_kws.py `
  models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/test_wavs/3.wav
```

预期输出包含 `audio`、`RTF`、`hits`、`silence control` 和 `resource release OK`。`RTF < 1` 才表示实时性达标；静音应为 `clean (no false trigger)`。测试音频使用模型自带的 `test_keywords.txt` 时，需要把脚本中的 `keywords_file` 切换到该文件；它不是“你好问问”的真实录音。

模型已下载时，也可以运行隔离 provider 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sherpa_provider.py -q
```

## TTS→VAD→KWS 闭环验证

适用时机：更换 TTS/KWS/VAD 模型、音频重采样逻辑、关键词文件或 Sherpa 版本后。该例程不打开麦克风，用 TTS 合成的“你好问问”回灌唤醒模型。

```powershell
.\.venv\Scripts\python.exe tmp_proto/tts_kws_vad.py
```

预期 JSON 输出中 `hit: true`、`kws_hits` 包含 `你好问问`、`vad_segments` 非空，并写出 `tmp_proto/tts_nihao.wav`。注意使用隔离 `.venv` 的 Python，因为它安装了 sherpa-onnx 和 soundfile；系统 Python 没有这些依赖。

## 本地麦克风与 VAD 冒烟

适用时机：更换电脑、音频驱动、输入设备、`sounddevice` 版本或 Silero 模型后。音频只保存在内存中，不保存、不上传。

先列出设备：

```powershell
.\.venv\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"
```

再用输入设备编号运行短采集：

```powershell
.\.venv\Scripts\python.exe scripts/smoke_microphone.py --device 1 --duration 3
```

预期结果包含 `REAL_MICROPHONE_LOCAL`、非负 RMS/peak、`audio_saved: false` 和 `resources_released: true`。安静环境中 `vad_segments` 为空是正常结果；该命令只证明设备、采集和 VAD 管线可用，不证明真实唤醒质量。

## 真实麦克风唤醒验收

适用时机：更换麦克风、唤醒词、关键词阈值或 Sherpa 版本后。用**系统 Python**（已装 sounddevice/sherpa-onnx/numpy）运行：

```powershell
python tmp_proto/live_wake.py --duration 45 --device 1
```

运行后对着麦克风说「你好问问」。预期立即打印 `WAKE HIT: '你好问问'`，结束 JSON 中 `hit: true`、`audio_saved: false`、`resources_released: true`。可用 `--threshold` 调整灵敏度（默认 0.25），`--device` 从设备列表中选择。

## 模型下载与完整性检查

适用时机：首次设置、清理缓存后恢复、或模型文件不完整时。必须使用固定版本 URL，并在解压前确认归档完整。

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:12334"
$env:HTTPS_PROXY = "http://127.0.0.1:12334"
$env:ALL_PROXY = "http://127.0.0.1:12334"

curl.exe -C - -L -o models/tts.tar.bz2 `
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2
7z t models/tts.tar.bz2
7z x models/tts.tar.bz2 -omodels
```

若代理没有监听，先检查：

```powershell
Test-NetConnection 127.0.0.1 -Port 12334
```

`file ends unexpectedly` 或 `7z t` 失败表示下载不完整，不能解压或把它当成模型使用。恢复前保留部分文件，使用 `curl -C -`；如果服务器不接受续传，删除该文件后重新下载。

## 桌面前端改动

适用时机：修改 `desktop/src/`、`desktop/index.html`、Vite 配置或 Tauri 前端依赖后。

```powershell
Push-Location desktop
npm run build
Pop-Location
```

该命令同时运行 TypeScript 检查和 Vite 生产构建。启动本地页面进行手工检查时：

```powershell
Push-Location desktop
npm run dev
Pop-Location
```

Vite 应使用 `http://localhost:<port>`，不要把 `127.0.0.1` 作为视觉验证入口。

## Tauri/Rust 改动

适用时机：修改 `desktop/src-tauri/`、窗口属性、透明度、always-on-top、skip-taskbar 或系统 API 后。

```powershell
Push-Location desktop/src-tauri
cargo check
Pop-Location
```

打包前再运行：

```powershell
Push-Location desktop
npm run tauri build
Pop-Location
```

透明窗口、DPI、多显示器和不抢焦点行为不能只靠 `cargo check` 判定，必须在 Windows 实机启动后验收。

## EvoX 会话桥接回归

适用时机：修改 `core/session_bridge.py`、认证头、桥接 URL、turn 取消路径或响应格式后。

```powershell
python -m pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q
```

桥接必须携带 bearer token；明文 HTTP 只允许 `localhost` 或 IP loopback；HTTPS 可用于远端端点；`turn_id` 会进行 URL 编码。真实 EvoX 服务尚未在本机提供可验证的测试端点，因此当前测试使用本地临时 HTTP 服务。

## 诊断与设备检查

适用时机：用户反馈“没有唤醒”“没有麦克风”“会话不回消息”时。

```powershell
python -c "from evox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose(), ensure_ascii=False, indent=2))"
```

诊断只输出 provider、桥接 URL 是否配置 token、音频后端可用性和设备列表，不输出 token 内容。设备枚举失败时先看 `reason`，再决定是否安装 `sounddevice` 或系统音频驱动。

## 修改前后的工作区检查

适用时机：开始一轮较大修改、准备提交或接手隔夜工作区时。

```powershell
git status --short
rg --files -g '!desktop/node_modules' -g '!.venv' | sort
rg "TODO|FIXME|release blocker|not verified" core evox_plugin desktop docs tests scripts
```

先确认已有用户修改，不要覆盖无关脏文件；再根据未验证项选择最小回归范围。

## 推荐执行顺序

- 小型 Python 修改：日常最小回归。
- 事件或状态修改：契约回归，然后日常最小回归。
- 音频/provider 修改：Provider 回归、KWS 隔离验证，然后日常最小回归。
- 前端修改：`npm run build`；窗口属性修改再加 `cargo check` 和 Windows 实机验收。
- 模型或依赖变更：记录版本、来源、归档校验结果到 `THIRD_PARTY_NOTICES.md` 和 `docs/research/prototype-results.md`。
