# 测试文档(Testing)

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-08-02
> 配套 [routines.md](routines.md)(改完什么跑什么)与 [prototype-results.md](research/prototype-results.md)(实测数据)。

## 1. 验证等级定义

这是本项目最重要的测试纪律:**不得把低等级证据当高等级用**。

| 等级 | 含义 | 能证明 | 不能证明 |
|---|---|---|---|
| **DOC** | 上游文档或他仓报告,本地未复现 | 参考价值 | 本机可用性 |
| **AUTO** | 本地自动化测试/脚本,确定性,无外部硬件 | 代码逻辑正确 | 真实设备行为 |
| **SIM** | 模拟输入(mock 传输/合成音频/headless 浏览器/mock 子进程) | 代码路径连通 | 真机表现 |
| **REAL-MIC** | 本机真实麦克风 | 音频链路真实可用 | 多环境质量 |
| **REAL-AGENT** | 真实外部 agent 进程被拉起并完成一轮 | 第三方 agent 接入真实可用 | 多 agent 并发表现 |
| **REAL-EVOX** | 真实 EvoX 会话 | 端到端业务闭环 | — |
| **REAL-WIN** | 真实 Tauri/WebView2 窗口 | 透明/DPI/多屏/RDP | — |

**REAL-AGENT 为 2026-08-02 新增**(Phase 4 平台化)。含义严格:真实的外部 agent 进程被拉起、真实产出增量、真实完成一轮。**mock 子进程只算 SIM** —— 这是红线 3 的直接推论,`cli.py` 的解析测试再全也不能升级成 REAL-AGENT。

**当前达成**:DOC / AUTO / SIM 已建立,REAL-MIC 有一次唤醒验证。**REAL-AGENT / REAL-EVOX / REAL-WIN 完全空白**,构成主要发布风险。

## 2. 测试环境

### 2.1 环境记录(2026-07-28 实测)

| 项 | 版本 |
|---|---|
| 操作系统 | Windows 11 Pro (10.0.26200) |
| Python(隔离 `.venv`) | 3.12.10 |
| sherpa-onnx / sherpa-onnx-core | 1.13.4 |
| numpy | 2.5.1 |
| sounddevice | 0.5.5 |
| soundfile | 0.14.0 |
| pytest | 9.1.1 |
| Node.js | v24.11.1 |
| npm | 11.7.0 |
| TypeScript | 5.6.x |
| Vite | 8.1.x |
| Tauri | 2.x |

### 2.2 环境搭建

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` 通过 `-r requirements-voice.txt` 引入固定的语音运行时依赖,并额外固定 `pytest==9.1.1`;生产运行环境可只安装 `requirements-voice.txt`。

### 2.3 模型依赖

模型**不由 pip 安装**,需单独准备,总计约 413 MB:

| 模型 | 路径 | 体积 | 用途 |
|---|---|---:|---|
| KWS Zipformer | `models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/` | 36 MB | 中文唤醒 |
| Silero VAD | `models/silero_vad.onnx` | 2.3 MB | 端点检测 |
| MeloTTS VITS | `models/vits-melo-tts-zh_en/` | 183 MB | 中英合成 |
| (未清理归档) | `models/kws.tar.bz2` + `tts.tar.bz2` | 192 MB | 可删除 |
| 声纹 3D-Speaker ERes2Net | `models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` | 37.8 MB | 声纹准入(**已下载**,dim 512) |

Silero VAD SHA-256:`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`
声纹模型 SHA-256:`1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b`(39,593,761 字节,embedding dim **512**,2026-08-02 下载并自检)

声纹模型来源(核实等级:**官方文档确认**,k2-fsa.github.io):`https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`。release tag 里 `recongition` 的拼写是官方笔误,不是本文档写错。

模型-测试的分工是有意设计的:**`tests/test_speaker.py` 与 `tests/test_speaker_privacy.py` 的 30 个用例全部不依赖此模型** —— 需要守的性质(fail-closed、音频不落盘、`describe()` 不含向量)恰恰是模型缺失时必须成立的那些。只有 `tests/integration/test_speaker_model.py` 需要权重,模型缺失时它 skip 而不是 fail。

## 3. 测试分层

```
      ┌─────────────────────────────────┐
      │  L5 真机验收 REAL-*  ← 空白最多   │  发布阻塞
      ├─────────────────────────────────┤
      │  L4 端到端模拟 SIM               │  e2e_simulated.py
      ├─────────────────────────────────┤
      │  L3 集成测试 AUTO                │  t10 harness / 桥接 HTTP
      ├─────────────────────────────────┤
      │  L2 契约测试 AUTO                │  schema / 状态机
      ├─────────────────────────────────┤
      │  L1 单元测试 AUTO                │  provider / plugin 工具面
      └─────────────────────────────────┘
```

### L1–L2 单元与契约(`tests/`,11 文件 1823 行,161 用例)

| 文件 | 用例数 | 覆盖内容 |
|---|---:|---|
| `test_event_schema.py` | 1 | 产出事件符合 JSON Schema 必填结构 |
| `test_events.py` | 8 | 契约枚举从文件读取(9 种类型)、信封形状、payload 默认、id 唯一性、四种契约违规被拒 |
| `test_agent_event_schema.py` | 34 | `voice-events.schema.json` **SHA-256 摘要钉死**;平台契约 12 种类型分四组;两信封同形且枚举互斥;`contract_for()` 全类型解析与未声明类型报错;`validate_any_event()` 双流通过、送错契约仍失败;agent 注册契约 16 条拒绝路径;`kind` 枚举与 `AGENT_KINDS` 一致;**schema 不许超出校验器实现的关键字子集** |
| `test_voice_contract.py` | 2 | 生命周期契约;非法提交与取消被拒 |
| `test_plugin_tools.py` | 16 | pause/resume 门控、采集生命周期、启动失败回滚、合成唤醒标记、完整回合契约、传输层接线、诊断不泄漏 token、设备枚举;**记忆接线 6 项**:两侧轮次都入库并带 `role:*` 标签、不 attach 就没有数据库文件、writer 抛异常不打断对话、凭据话术经真实 `submit_text` 也进不了库、`diagnose()["memory"]` 报计数不报文本、未接记忆时告警 |
| `test_memory.py` | 62 | store 与协议一致、构造不开文件、schema 版本与幂等 close、**无 BLOB 列**、`MemoryRecord` 无 `bytes` 字段、未知 scope/kind 与空文本被拒、`bytes` 抛 `TypeError`、forget 连索引一起删;三层各归其位、`memory.written` 属平台契约而非语音契约、事件不带文本、召回只报计数、prune 保留最新;去重(同一事实、3 种近似变体、拉丁大小写、标签合并、轮次与审计**不去重**、fingerprint 边界);召回(中文可搜、精确到能返回空、拉丁忽略大小写、limit 与 scope、**麦克风里的 FTS 语法不炸**、轮次时间序、严格胜过宽松、两侧分词一致);长期层成功率(无观测返回 `None`、按审计标签算出 2/3);凭据过滤(9 样本整条拒绝且无事件无残留、5 句日常话不误伤、每个模式都有名字);Markdown 镜像(落文件带 front matter、手改在下一次召回生效、裸文件被收录并写回 id、文件里的凭据被拒、只有 `prune=True` 才删索引、无目录时是 no-op、未闭合 front matter 当正文);配置(默认值、`open_memory` 三件套、未来 schema 版本被拒、单文件、**`.gitignore` 里 `/memory/` 的前导斜杠**——不带斜杠会连 `core/memory/` 的源码一起忽略) |
| `test_speaker.py` | 11 | store 往返/原子写/版本拒绝/损坏 JSON 拒绝;fail-closed 四条路径;`describe()` 不含向量;`remove()` 幂等 |
| `test_speaker_privacy.py` | 19 | 环形缓冲窗口语义与超长块;**AST 断言缓冲无文件系统面**;全周期空目录断言;决策后窗口即丢;四条 fail-closed `start()` 拒绝;校验异常算拒绝;拒绝不改状态不发回复;`diagnose()` 报计数不报向量、门关时告警;`enrollment/` 在 `.gitignore` 内;缺配置仍默认要求校验 |
| `test_provider_adapter.py` | 2 | VoxCord 加载与 VAD 回退契约(**无 VoxCord 时 skip**) |
| `test_session_bridge.py` | 3 | token 与 loopback 强制、认证发送、缺 turn_id 判失败 |
| `test_sherpa_provider.py` | 3 | 缺模型不导入运行时、真实模型加载且静音无命中、VAD 拒静音识语音 |

### L3 集成(`tests/integration/`,2 文件 10 用例)

`test_voice_stack.py`(5 用例 121 行)—— 原 `tmp_proto/t10_voice_stack_validation.py` 的**行为断言部分**已升格为默认收集的正式测试(2026-08-02,消化 §7 自列的「`tmp_proto/` 与 `tests/` 边界模糊」债务):

1. 双后端驱动同一回合路径(红线 2)
2. 打断取消同时到达传输层与状态机
3. 一整轮的每个事件都过 `contracts/voice-events.schema.json`
4. KWS 模型加载与释放(**无模型时 skip**)
5. VAD 模型加载与释放(**无模型时 skip**)

脚本本体留在 `scripts/acceptance/t10_voice_stack_validation.py` 作为**证据生成器**(打印计时与 RTF,喂 `docs/research/prototype-results.md`),它仍覆盖那六项整合检查:

1. Windows 启动与模型加载(计时)
2. 合成音频中文唤醒命中
3. 8 轮开合资源释放(tracemalloc 监控)
4. 12 秒连续静音流(误触与 RTF)
5. 会话后端可替换(双 mock 传输行为一致)
6. 可打断 TTS(speaking 中 cancel)

`test_speaker_model.py`(5 用例)—— 需要 37.8 MB 声纹模型,**缺模型时整文件 skip**。这是唯一需要权重的声纹测试:

1. 模型加载并报告 dim 512
2. 7 段真实人声的余弦矩阵可分:簇内最低 **0.736** > 簇间最高 **0.370**,默认阈值 0.5 落在间隙内
3. 注册 0 号后 1、2 号放行,3–6 号被拒且 `reason` 含 `below threshold`
4. 追加样本不丢先前注册,`samples_per_speaker` 计到 2
5. **反向断言**:合成音调(120 Hz vs 240 Hz)仍互相通过(0.767),证明合成音频**不能**用来测判别力 —— 这条测试存在的意义是让这个陷阱留在文档里,而不是被后人重新踩一遍

### L4 端到端模拟(`scripts/e2e_simulated.py`)

链路:唤醒 → ASR 文本 → 桥接发送 → 回复 → TTS 事件 → 连续对话 → 取消 → 停止。产出 17 事件、2 次桥接发送、1 次取消。**mock 传输,无麦克风、无真实 EvoX**。

### L5 真机(部分空白)

需要真实硬件的脚本统一放 `scripts/acceptance/`,**不进 pytest 默认收集**:

| 脚本 | 等级 | 状态 |
|---|---|---|
| `scripts/acceptance/smoke_microphone.py` | REAL-MIC | ✅ 设备开合与 VAD 管线可用 |
| `scripts/acceptance/live_wake.py` | REAL-MIC | ✅ 一次 `你好问问` 命中 |
| `scripts/enroll_speaker.py` | REAL-MIC | ⚠️ 已实现,**需你本人到场读 3–5 句** |
| (无) 声纹本人通过 / 他人拒绝 / 录音回放 | REAL-MIC | ❌ 模型已就位,缺本人录入与第二个人(P10) |
| (无) 真实 agent CLI 联调 | REAL-AGENT | ❌ 缺适配器(P5) |
| (无) 真实 EvoX 联调 | REAL-EVOX | ❌ 缺脚本与端点 |
| (无) 真机窗口验收 | REAL-WIN | ❌ 缺清单与脚本 |

## 4. 测试命令索引

| 目的 | 命令 | 等级 | 预期 |
|---|---|---|---|
| Python 全量 | `.venv\Scripts\python.exe -m pytest tests -q` | AUTO | 169 passed, 2 skipped |
| 记忆 | `.venv\Scripts\python.exe -m pytest tests/test_memory.py -q` | AUTO | 62 passed(**无需模型**) |
| 声纹(免模型) | `.venv\Scripts\python.exe -m pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | AUTO | 30 passed(**无需模型**) |
| 声纹(真实模型) | `.venv\Scripts\python.exe -m pytest tests/integration/test_speaker_model.py -q` | AUTO | 5 passed(缺模型时 5 skipped) |
| 声纹录入 | `.venv\Scripts\python.exe scripts/enroll_speaker.py --name <名字>` | REAL-MIC | 写入向量,不写音频 |
| 声纹注册状态 | `.venv\Scripts\python.exe scripts/enroll_speaker.py --name x --list-only` | AUTO | 只出名字与样本数 |
| 事件契约(语音) | `.venv\Scripts\python.exe -m pytest tests/test_events.py tests/test_event_schema.py -q` | AUTO | 9 passed |
| 事件契约(平台 + 注册) | `.venv\Scripts\python.exe -m pytest tests/test_agent_event_schema.py -q` | AUTO | 34 passed |
| 集成 | `.venv\Scripts\python.exe -m pytest tests/integration -q` | AUTO+SIM | 10 passed(缺模型时 3 passed, 7 skipped) |
| 语音冒烟 | `.venv\Scripts\python.exe scripts/smoke_voice.py` | SIM | 打印生命周期事件 |
| 端到端模拟 | `.venv\Scripts\python.exe scripts/e2e_simulated.py` | SIM | `E2E SIMULATED OK` |
| t10 栈验证(证据生成) | `.venv\Scripts\python.exe scripts/acceptance/t10_voice_stack_validation.py` | AUTO+SIM | `t10 OK` |
| TTS→VAD→KWS | `.venv\Scripts\python.exe tmp_proto/tts_kws_vad.py` | SIM | `hit: true` |
| KWS 隔离 | `.venv\Scripts\python.exe tmp_proto/test_kws.py <wav>` | AUTO | RTF < 1,静音 clean |
| 设备枚举 | `.venv\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"` | REAL-MIC | 列出输入设备 |
| 麦克风冒烟 | `.venv\Scripts\python.exe scripts/acceptance/smoke_microphone.py --device 1 --duration 3` | REAL-MIC | `audio_saved: false` |
| 真机唤醒 | `.venv\Scripts\python.exe scripts/acceptance/live_wake.py --duration 45 --device 1` | REAL-MIC | `WAKE HIT` |
| 诊断 | `.venv\Scripts\python.exe -c "from evox_plugin import VoicePlugin; import json; print(json.dumps(VoicePlugin().diagnose(), ensure_ascii=False, indent=2))"` | AUTO | 不含 token 内容 |
| 前端构建 | `cd desktop; npm run build` | AUTO | tsc + vite 通过 |
| Rust 检查 | `cd desktop/src-tauri; cargo check` | AUTO | 零警告 |
| 渲染路线原型 | 起 http server 后驱动 `window.__SPIKE__` | SIM | 三路线 FPS 数据 |

## 5. 已验证结果汇总

### 5.1 语音链路(t10,2026-07-28)

| 检查项 | 实测值 | 判定 |
|---|---|---|
| KWS 模型加载 | 0.627 s | ✅ < 2s |
| VAD 模型加载 | 0.053 s | ✅ < 2s |
| 合成 `你好问问` 唤醒 | 命中,解码 0.026 s | ✅ |
| TTS 生成 | 0.235 s(RTF ~0.3) | ✅ < 0.5 |
| 8 轮开合内存增长 | 0.01 MB | ✅ 无泄漏 |
| 12 s 静音误触 | 0 次,RTF 0.0186 | ✅ |
| 双后端行为一致 | sent/state 相同,turn_id 不同 | ✅ |
| speaking 中打断 | `turn.cancelled`,传输层收到取消 | ✅ |

### 5.2 渲染路线(t11,2026-07-28,headless Chromium,DPR 1)

| 路线 | FPS(2s 窗口) | 判定 |
|---|---:|---|
| CSS-only(降级档) | 239.8 | ✅ 采用为降级档 |
| Canvas 2D(v1 主路径) | 239.9 | ✅ 采用为主路径 |
| WebGL shader(v2) | 240.0 | ✅ 保留为升级路径 |

附带证据:WebGL 编译链接无错;amplitude 输入 `[0,0.5,1,0.5,0]` 钳制为 `[0.12,0.5,1,0.5,0.12]`;`prefers-reduced-motion`、`hardwareConcurrency`(32)、Canvas/WebGL context、`devicePixelRatio` 均可读;零 console 错误;三路线截图各异。

### 5.3 真机麦克风(2026-07-26)

| 检查项 | 结果 |
|---|---|
| 口述 `你好问问` | 7.193 s 命中,score 1.0 |
| 音频落盘 | `audio_saved: false` |
| 资源释放 | `resources_released: true` |
| 3 s 安静采集 | RMS 0.000044,peak 0.001068,无 VAD 段(安静环境正常) |

### 5.4 安全与边界(桥接)

| 攻击面 | 防护结果 |
|---|---|
| 无 token | 拒绝(`BridgeError`) |
| 明文 HTTP 非 loopback | 拒绝 |
| `localhost.evil.example` | 拒绝(解析后判定) |
| URL 内嵌凭据 | 拒绝 |
| turn_id 路径注入 | `quote(safe="")` 编码 |
| 响应缺 turn_id | 判失败 |
| 诊断输出 | 仅 `token_configured: bool` |

### 5.5 声纹与平台层(AUTO,2026-08-02)

下表**不依赖 37 MB 声纹模型**,这是有意的:要守的性质恰恰是模型缺失时必须成立的那些 —— 一个模型缺失就静默放行的门比没有门更糟。需要权重的结论单列在 §5.6。

| 检查项 | 结果 |
|---|---|
| sherpa-onnx 1.13.4 含完整声纹 API | ✅ 直接读已安装包的 API 面确认,零新依赖 |
| store 往返 / `version` 字段 / `dim` 字段 | ✅ |
| store 原子写 | ✅ 无残留 `*.tmp` |
| store 拒不认识的 `version` | ✅ `ProviderUnavailable: unsupported version` |
| store 拒损坏 JSON | ✅ `ProviderUnavailable: unreadable` |
| 模型缺失 → `load()` 报不可用而非抛异常 | ✅ |
| 模型缺失 → `verify()` 拒绝 | ✅ fail-closed |
| 无人注册 → `verify()` 拒绝 | ✅ fail-closed |
| 音频短于下限 → `embed()` 拒绝 | ✅ |
| 空名字注册被拒 | ✅ |
| `describe()` 不含任何向量值 | ✅ 逐值断言不出现在序列化输出里 |
| `remove()` 删除且幂等 | ✅ 第二次返回 `False` |
| 环形缓冲只留最近窗口 / 超长块不崩 / `clear()` 丢音频 | ✅ |
| 环形缓冲**无文件系统面** | ✅ AST 解析断言:只 import `numpy`/`typing`,无 `open`/`tofile`/`socket`/`Popen` 等标识符 |
| 一整轮门周期后工作目录**为空** | ✅ `chdir(tmp_path)` 后 31 次回调 + 一次命中,目录零文件 |
| 决策后校验窗口立即丢弃 | ✅ `len(ring) == 0` |
| 要求校验但无 verifier → `start()` 拒绝 | ✅ 且不留开着的设备 |
| 无人注册 → `start()` 拒绝 | ✅ |
| 模型缺失 → `start()` 拒绝 | ✅ |
| 校验器抛异常 → 记为拒绝 | ✅ `verifier error`,不是放行 |
| 低分拒绝**静默**:不改状态、不发回复 | ✅ 状态机原地不动,只多一条 `wake.rejected` |
| 门关闭时 `diagnose()` 出显式告警 | ✅ `anyone can wake the platform` |
| `diagnose()` 报计数与名字,不报向量 | ✅ `repr` 里无 `raw`,无 `embeddings`/`vectors` 键 |
| `enrollment/` 在 `.gitignore` 内 | ✅ 解析行断言,不是子串搜索 |
| 缺配置文件仍默认 `require_verification=True` | ✅ 降级路径不是保护静默关闭的时机 |
| 事件类型枚举从契约文件读取 | ✅ 9 种,Python 里不镜像 |
| 四种契约违规被 `validate_event()` 拒 | ✅ 未知 type / 错 version / 多余字段 / 缺必填键 |
| `voice-events.schema.json` 字节不变 | ✅ **SHA-256 摘要钉死**(575 字节,`4f60…5de5`) |
| 平台契约 12 种类型,分 `task`/`agent`/`tool`/`memory` 四组 | ✅ 逐组断言,不只断言总数 |
| 两个信封可合流 | ✅ 必填键、属性集、`version` 常量、`additionalProperties` 全同;两个 `type` 枚举**互斥** |
| `contract_for()` 对每个已声明类型解析到正确文件 | ✅ 21 种类型逐一;未声明类型报错;同名被两个契约声明也报错 |
| 送错契约的事件仍失败 | ✅ 平台事件对着语音契约校验 → `not in the contract enum` |
| agent 注册契约拒坏配置 | ✅ 16 条:缺 `name`/`kind`、`kind` 不在枚举、`cost` 越界、`True` 冒充整数、负 `latency_ms`、`timeout_s` 为 0、`args` 类型错、未知键、顶层四种 |
| `kind` 枚举与 `AGENT_KINDS` 一致 | ✅ 测试锁死,避免配置校验通过后在构造适配器时才炸 |
| schema 不超出校验器实现的关键字子集 | ✅ 反向断言:声明了却不生效的关键字比不声明更糟 |
| 平台层四包导入无副作用 | ✅ 不启子进程、不开套接字 |

### 5.6 声纹判别力与延迟(AUTO,需模型,2026-08-02)

| 检查项 | 结果 |
|---|---|
| 模型自检 | dim **512**,SHA-256 `1a33…5e4b`,39,593,761 字节 |
| 冷加载耗时 | **0.234 s** |
| 注册耗时(1 段 5.61 s) | 0.166 s |
| **校验耗时(1.5 s 窗口,12 次)** | 中位数 **41 ms**(min 39.8 / max 42.7)—— NFR-1.9 目标 < 300 ms,达标 |
| 真实人声簇内最低相似度 | **0.736**(7 段分三簇) |
| 真实人声簇间最高相似度 | **0.370** |
| 默认阈值 0.5 是否落在间隙 | ✅ 下侧余量 0.24,上侧 0.13 |
| 注册一人后同人放行 / 他人拒绝 | ✅ 2/2 放行,4/4 拒绝 |
| 合成音调能否替代人声测判别力 | ❌ **不能**:120 Hz vs 240 Hz 仍互通(0.767) |

**这一节是 AUTO,不是 REAL-MIC。** 音频是模型自带的真实人声录音,不是本机麦克风采集。它能证明「模型确实能分辨人」并给阈值 0.5 提供依据;它**不能**证明本人通过率、他人拒绝率或回放行为 —— 那三项仍是第 17–19 项待验收。

### 5.7 记忆(AUTO,2026-08-02,P3)

| 检查项 | 结果 |
|---|---|
| FTS5 是否可用 | ✅ SQLite **3.49.1**,`ENABLE_FTS5` 在编译选项里 |
| **FTS5 默认分词器能否搜中文** | ❌ **不能**(实测):同一行里 `MATCH 'english'` 命中、`MATCH '中文'` 不命中 —— `unicode61` 把整段连续汉字当一个 token |
| 派生 token 列后中文能否搜到 | ✅ 「中文」找到「用户喜欢用中文交流」;「完全无关的查询」返回空 |
| 索引侧与查询侧分词是否一致 | ✅ 索引 = 单字 + 相邻双字;查询 = 只用双字,所以「偏好」不会匹配只含「好」的记录 |
| 麦克风里的 FTS 语法 | ✅ `NOT 中文` / `中文 OR *` / `"unbalanced` / `中文 AND (` / 空串都不抛异常 |
| `records` 表列类型 | ✅ 全部 `TEXT` 或 `INTEGER`,**无 BLOB** —— 音频没有列可落 |
| `bytes` 入库 | ✅ `store.write()` 与 `writer.write_turn()` 都抛 `TypeError` |
| 凭据形状文本 | ✅ 9 个样本**整条拒绝**,`count() == 0`,不发事件,`last_refusal` 与 `describe()` 里都找不到原文 |
| 日常话是否误伤 | ✅ 5 句(含「我的密码忘了怎么办」「token 是什么意思」)全部放行 |
| 记忆事件是否带文本 | ✅ 只有 id / scope / kind / 计数 / 标签;`memory.written` 在平台契约里、不在语音契约里 |
| 去重范围 | ✅ 仅中期事实层(部分唯一索引);两句相同的轮次、两条相同的审计都保留 |
| Markdown 往返 | ✅ 手改 `memory/facts/*.md` → `sync_facts()` → 下一次召回跟着变;裸文件被收录并写回 id;文件里的凭据被拒;删文件只在 `prune=True` 时删索引 |
| 单文件 | ✅ 只有 `memory.db` 加 SQLite 自己的 `-wal`/`-shm`/`-journal` |
| 无观测时的成功率 | ✅ `rate` 是 `None` 而不是 `0.0` |
| 记忆接进语音路径 | ✅ 两侧轮次都入库并带 `role:*`;不 attach 就不产生数据库文件;writer 抛异常不打断回合 |
| `/memory/` 是否锚定 | ✅ 前导斜杠已加,并有测试解析 `.gitignore` 双向断言 —— 不带斜杠时 `git check-ignore` 实测把 `core/memory/store.py` 也算进忽略 |
| 用例数 / 耗时 | 62 passed in 0.69 s(`tests/test_memory.py`) |

**跨进程重启未验。** 上面的 Markdown 往返全部发生在一个进程内。「关掉程序重开,事实还在」是第 25 项待验收,等级 REAL,不是 AUTO 能关的。

## 6. 待验收矩阵(release gate)

| # | 待验收项 | 目标等级 | 需要的测试资产 | 当前 |
|---|---|---|---|---|
| 1 | 唤醒质量(安静) | REAL-MIC | 多次重复统计脚本 | ❌ 缺 |
| 2 | 唤醒质量(远场 3–5 m) | REAL-MIC | 同上 | ❌ 缺 |
| 3 | 唤醒质量(噪声/音乐) | REAL-MIC | 噪声场景清单 | ❌ 缺 |
| 4 | 误触率(长时静默/日常对话) | REAL-MIC | ≥1 h 静默跑 | ❌ 缺 |
| 5 | Silero 真机端点 | REAL-MIC | 语音起止断言脚本 | ❌ 缺 |
| 6 | EvoX 发送/增量回复 | REAL-EVOX | 真实端点 + 联调脚本 | ❌ 缺 |
| 7 | EvoX 取消/超时/重连 | REAL-EVOX | 故障注入用例 | ❌ 缺 |
| 8 | 流式首字延迟 | REAL-EVOX | 计时埋点 | ❌ 缺 |
| 9 | 透明合成(真实 WebView2) | REAL-WIN | 启动 + 目视清单 | ❌ 缺 |
| 10 | DPI 125/150/175% | REAL-WIN | 缩放切换清单 | ❌ 缺 |
| 11 | 多显示器定位 | REAL-WIN | 主副屏切换 | ❌ 缺 |
| 12 | 不抢焦点/点击穿透 | REAL-WIN | 焦点断言 | ❌ 缺 |
| 13 | 托盘常驻与退出 | REAL-WIN | 功能未实现 | ❌ 缺 |
| 14 | RDP 软件渲染降级 | REAL-WIN | 远程桌面会话 | ❌ 缺 |
| 15 | ≥30 min 资源画像 | REAL-WIN | CPU/内存/FPS 采样器 | ❌ 缺 |
| 16 | 打包产物可安装运行 | REAL-WIN | NSIS/MSI 安装验证 | ❌ 缺 |
| 17 | 声纹本人通过率(安静/远场/噪声) | REAL-MIC | ~~模型~~ + 本人录入 + 重复统计 | ❌ 缺(模型与录入 CLI 已就位,**待你本人到场**) |
| 18 | 声纹他人拒绝(**球不弹、无任何输出**) | REAL-MIC | 第二个人配合 | ❌ 缺 |
| 19 | 声纹录音回放攻击 | REAL-MIC | 录音回放 + 结果诚实记录 | ❌ 缺(**本轮不做反欺骗模型**,见 ADR 002 局限) |
| 20 | 音频不落盘断言进默认套件 | AUTO | 采集路径写盘断言 | ✅ **已完成**(`test_a_full_gate_cycle_writes_nothing_to_disk` + 环形缓冲 AST 断言) |
| 21 | agent 适配器真机(每种 kind 一项) | REAL-AGENT | 已装且已登录的 CLI | ❌ 缺(P5/P7) |
| 22 | 路由在真实延迟下的表现 | REAL-AGENT | 计时埋点 + 多 agent | ❌ 缺(P6) |
| 23 | `shell.run` 确认流程实机验收(含拒绝路径) | REAL-WIN | 唤醒球确认 UI | ❌ 缺(P4/P8) |
| 24 | 误唤醒触发工具执行的防护验证 | REAL-MIC | 攻击面用例 | ❌ 缺(P4) |
| 25 | 记忆跨会话持久性 | REAL | 重启后召回 + 手改 Markdown 生效 | ❌ 缺(P10;同进程往返已 AUTO) |
| 26 | 唤醒球运行时显隐(`show_orb`/`hide_orb`) | REAL-WIN | 功能未实现 | ❌ 缺(P8) |

**26 项里 1 项已完成(第 20 项,P1 交付),25 项待验收**。这是从「原型可用」到「可发布」之间的真实距离。第 17–26 项是 Phase 4 平台化新增的:声纹三项、隐私断言一项、agent 与路由两项、工具安全两项、记忆一项、唤醒球一项 —— 其中唯一能纯 AUTO 关掉的就是隐私断言那一项,其余全部需要真实硬件或真实第三方进程。

## 7. 测试债务与改进建议

| 优先级 | 债务 | 建议 |
|---|---|---|
| 高 | 无 REAL-AGENT / REAL-EVOX / REAL-WIN 任何资产 | Phase 4 各阶段落地前先写验收清单与脚本骨架 |
| 中 | 开发依赖与运行时依赖需保持同步 | 更新语音依赖时同步验证 `requirements-dev.txt` 的递归安装 |
| 中 | 无覆盖率统计 | 引入 `pytest-cov`,对 `core/` 设阈值 |
| 中 | 平台层契约无「类型不含 SDK」的自动断言 | P5 起用 `typing.get_type_hints` 遍历三个 dataclass 断言字段类型,替掉现在靠构造与评审的保证 |
| 低 | 无 CI | 单机项目可暂缓,但建议本地 pre-commit 钩子 |
| 低 | 前端无单元测试 | Phase 4 渲染器落地后补 vitest |

**已消化的债务**(2026-08-02):

- ~~无 git 仓库,测试结果无法与代码版本关联~~ → 已 `git init`,基线 `9f7d923`;后续实测数据带 commit hash
- ~~`tmp_proto/` 与 `tests/` 边界模糊~~ → t10 的行为断言升格为 `tests/integration/test_voice_stack.py`,需真实硬件的脚本移入 `scripts/acceptance/`

## 8. 已知测试限制

1. **headless Chromium ≠ WebView2** — t11 的 FPS 与 WebGL 结论不能直接推广到真实 Tauri 窗口。
2. **合成音频 ≠ 真人语音** — TTS 回灌验证的是管线,不是唤醒质量。
3. **mock 传输 ≠ 真实 EvoX** — 证明了编排层与可替换性,不证明业务闭环。
4. **单主机验证** — 所有结论仅覆盖当前 Windows 11 + Realtek 设备组合。
5. **安静环境** — 现有麦克风测试均在安静环境,噪声鲁棒性未知。
6. **控制台中文乱码** — Windows 代码页显示问题,不是数据缺陷(UTF-8 字节正确)。
7. **mock 子进程 ≠ 真实 agent** — `cli.py` 的解析测试无论多全都只是 SIM,不能升级成 REAL-AGENT。
8. **无声纹模型时的声纹测试** — 覆盖的是 fail-closed 与 store 行为,**不覆盖识别准确率**。本人通过率与他人拒绝率必须 REAL-MIC 实测。
9. **声纹不防录音回放** — 本轮不做反欺骗模型,这是已知缺口(ADR 002 局限节),不是尚未测到。
10. **合成音频不能测声纹判别力** — 已实测:120 Hz 与 240 Hz 两组谐波栈互相通过(0.767),因为模型把两者都读成同一种「非人声」。任何用生成音调测判别力的测试都会**空过**。`test_synthetic_tones_cannot_stand_in_for_speech` 把这个陷阱钉住了。
11. **模型自带录音 ≠ 本机麦克风** — §5.6 的簇内 0.736 / 簇间 0.370 是真实人声,但是既有录音。它给阈值提供依据,等级是 AUTO,**不得当 REAL-MIC 报告**。
