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

当前基线 **43 passed, 2 skipped**（2 个 skip 是可选的 VoxCord 适配器）。有声纹模型时 `tests/integration` 的两个模型用例也会跑，无模型时它们 skip —— 所以 skip 数会随环境变化，**passed 数下降才是回归**。

## 契约或事件字段变更

适用时机：修改 `contracts/voice-events.schema.json`、事件类型、状态机或插件事件 payload 后。

```powershell
python -m pytest tests/test_event_schema.py tests/test_events.py tests/test_voice_contract.py tests/test_plugin_tools.py -q
```

同时检查 `contracts/voice-events.schema.json` 的枚举是否覆盖新增事件。**信封的唯一构造点是 `core/events.py`** —— 新事件类型只需加进契约文件，`allowed_types()` 在运行时读取，Python 侧不需要同步改动；`tests/test_events.py` 会在两者漂移时失败。事件版本变更时，更新协议文档和兼容性测试，不要只改生产代码。

平台事件走 `contracts/agent-events.schema.json`（P2 新增），`voice-events.schema.json` 保持字节不变、version 保持 `"1"`。

## Provider 适配器变更

适用时机：修改 `core/audio/`（`base` / `kws` / `vad` / `capture` / `voxcord`）、`core/providers.py` 重导出薄壳、VoxCord 动态导入、Sherpa 模型加载或设备发现后。

```powershell
python -m pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q
python -c "from core.providers import VoxCordAdapter; print(VoxCordAdapter().load())"
```

没有 VoxCord、Sherpa 或音频驱动时，导入仍应成功；不可用状态应返回原因，而不是在模块导入阶段崩溃。第二条命令走的是 `core/providers.py` 的重导出路径，它同时验证拆包后旧导入路径没有断。

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
.\.venv\Scripts\python.exe scripts/acceptance/smoke_microphone.py --device 1 --duration 3
```

预期结果包含 `REAL_MICROPHONE_LOCAL`、非负 RMS/peak、`audio_saved: false` 和 `resources_released: true`。安静环境中 `vad_segments` 为空是正常结果；该命令只证明设备、采集和 VAD 管线可用，不证明真实唤醒质量。

## 真实麦克风唤醒验收

适用时机：更换麦克风、唤醒词、关键词阈值或 Sherpa 版本后。用隔离 `.venv` 的 Python 运行（系统 Python 没装 sherpa-onnx / soundfile）：

```powershell
.\.venv\Scripts\python.exe scripts/acceptance/live_wake.py --duration 45 --device 1
```

运行后对着麦克风说「你好问问」。预期立即打印 `WAKE HIT: '你好问问'`，结束 JSON 中 `hit: true`、`audio_saved: false`、`resources_released: true`。可用 `--threshold` 调整灵敏度（默认 0.25），`--device` 从设备列表中选择。

## 声纹回归

适用时机：修改 `core/audio/speaker.py`、`core/audio/capture.py` 的环形缓冲或声纹门、`config/speaker.toml`、注册脚本，或调整阈值后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q
```

这组测试**故意不依赖 37 MB 声纹模型**：要守的性质恰恰是模型缺失时必须成立的那些。逐条必须成立的是 fail-closed（失败即关闭）四条路径 —— 模型缺失、无人注册、embedding 抛异常、分数低于阈值 —— 全部落在拒绝一侧，`verify()` 对普通拒绝从不抛异常而是返回 `accepted=False`。另外两条是隐私断言：`describe()` 不含任何向量值，音频不落盘。

**任何一条 fail-closed 断言变红都不许绕过。** 一个模型缺失就静默放行的声纹门，比没有门更糟 —— 它给了一种不存在的安全感。

改完阈值后同时看诊断：

```powershell
.\.venv\Scripts\python.exe -c "from evox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose()['speaker'], ensure_ascii=False, indent=2))"
```

预期只有名字与样本数，**绝不含向量**。若 `require_verification` 为 `False`，诊断必须把它报成警告。识别准确率（本人通过率、他人拒绝率、录音回放）只能 REAL-MIC 实测，AUTO 一律不背书。

## 工具权限回归

适用时机：修改 `core/tools/`（`contract` / `policy` / `fs` / `web` / `shell`）或 `config/tools.toml` 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_security.py -q
```

每条安全门对应一个测试，一条都不许跳过：`fs.read` 沙箱越界被拒、敏感文件名（`.env` / `*.pem` / `id_rsa*` / `credentials.json` / `*secret*`）被拒、`shell.run` 默认关闭、白名单外命令**被拒而非被询问**、每个危险模式（`rm -rf` / `git push --force` / `reset --hard` / `format` / `del /s`）被硬拦截。

`voice` 与 `agent` 两个 origin 必须走同一道 `ToolPolicy.check()`。新增工具时先确认 `policy.py` 已覆盖它，再写工具本体 —— 顺序反了就会出现一个没有门的工具。

## 记忆回归

适用时机：修改 `core/memory/`(`store` / `write` / `recall`)或记忆 schema 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -q
```

四组：写入、召回、去重、审计。两条红线断言：**音频永不入库**（契约里只有 `text` 字段，加断言守住），`asr.final` 入库前过敏感模式过滤（疑似密钥/token 不写入）。SQLite 必须保持单文件；改 schema 前先定迁移路径，不要靠删库重建。

中期事实同时落 `memory/facts/*.md`，人类可读层是事实来源、SQLite 是索引。手改 Markdown 后应能在下一次召回中体现 —— 这条只能真机验（重启进程）。

## agent 适配器回归

适用时机：修改 `core/agents/`(`contract` / `cli` / `evox` / `acp` / `http` / `openclaw`)或 `config/agents.toml` 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent_contract.py tests/test_agent_cli.py -q
```

契约测试守红线 2：`AgentDescriptor` / `Task` / `AgentChunk` 的字段类型只许 `str` / `int` / `float` / `frozenset` / `tuple` / `Mapping`，**任何 agent SDK 类型出现即失败**，事件 payload 同理。`cli.py` 用 mock 子进程测流式解析、超时与取消。

**mock 子进程只算 SIM。** 真实 CLI 端到端跑通一轮才是 REAL-AGENT，两者不得互相冒充。`evox.py` 包装后的行为必须与包装前的 `LocalEvoXTransport` 路径一致，`core/session_bridge.py` 的五道安全校验不得随之降级。

## 派发回归

适用时机：修改 `core/dispatch/`(`dispatcher` / `router` / `aggregator` / `intent` / `breaker`)后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_router.py tests/test_dispatcher.py -q
```

覆盖五维打分（能力/成本/延迟/成功率/负载）、熔断器开合、`single` / `race` / `fanout` 三模式、规则意图分类。双 mock agent 必须产出行为一致的回合 —— 这是红线 2 在派发层的落点。

**语音路径默认必须是 `single` 或 `race`。** 若默认变成 `fanout`，首字延迟会退化为最慢 agent 的延迟；这不是性能退步而是体感破坏，改动前先看 ADR 005。

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

声纹模型是单个 `.onnx`，不需要解压（约 37 MB）：

```powershell
curl.exe -C - -L -o models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx `
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx
```

release tag 里 `recongition` 的拼写是**官方笔误**，照抄即可，改成正确拼写会 404。下载后用 provider 自检而不是只看文件大小：

```powershell
.\.venv\Scripts\python.exe -c "from core.audio import SpeakerVerificationProvider; print(SpeakerVerificationProvider('models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx').load())"
```

预期 `available: True` 且 `dim` 为正整数。文件截断时 `load()` 会返回不可用及原因，**不会抛异常** —— 所以必须看返回值，不能靠「没报错」判定成功。

## 声纹录入

适用时机：首次录入本人声纹、追加样本提高通过率，或换麦克风后重录。**必须本人在场**。

```powershell
.\.venv\Scripts\python.exe scripts/enroll_speaker.py --name <你的名字>
```

按提示读 3～5 句，脚本显示每段时长并写入 embedding。`enroll` 是**追加**语义：已有向量保留、新向量附加，所以一次录得不好可以补录，不必全部重来。

注册数据落 `enrollment/voiceprints.json`，它是**生物特征**，已在 `.gitignore` 内，**永不提交**。查看注册状态只用 `describe()`，不要直接读文件：

```powershell
.\.venv\Scripts\python.exe -c "from core.audio import SpeakerVerificationProvider; import json; print(json.dumps(SpeakerVerificationProvider('models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx').describe(), ensure_ascii=False, indent=2))"
```

删除某人的注册：`provider.remove("<名字>")`，返回 `True` 表示删掉了、`False` 表示本来就没有。

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
- 声纹修改：声纹回归，然后日常最小回归；阈值改动额外看 `diagnose()`。
- 平台层修改：对应的工具/记忆/agent/派发回归，然后日常最小回归。
- 前端修改：`npm run build`；窗口属性修改再加 `cargo check` 和 Windows 实机验收。
- 模型或依赖变更：记录版本、来源、归档校验结果到 `THIRD_PARTY_NOTICES.md` 和 `docs/research/prototype-results.md`。

每个阶段收尾一律跑全量 `python -m pytest tests -q`（当前基线 **43 passed, 2 skipped**），不用单文件绿灯代替全量。
