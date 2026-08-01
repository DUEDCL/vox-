# 技术架构与组件边界

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-07-28
> 本文描述磁盘上**已实现**的架构。计划中但未落地的部分明确标注「未实现」。

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
│  evox_plugin/plugin.py   VoicePlugin:EvoX 工具面 + 回合编排│
└──────┬──────────────────────────────┬────────────────────┘
       │                              │
┌──────┴───────────────┐   ┌──────────┴────────────────────┐
│ 核心层 Core           │   │ 传输层 Transport               │
│ core/state.py        │   │ core/session_bridge.py         │
│   状态机              │   │   ConversationTransport 协议   │
│ core/providers.py    │   │   LocalEvoXTransport 实现       │
│   语音提供器           │   └──────────┬────────────────────┘
└──────┬───────────────┘              │
       │                              │
┌──────┴───────────────┐   ┌──────────┴────────────────────┐
│ 本地模型 models/      │   │ EvoX 会话(外部)                │
│ KWS / VAD / TTS      │   │ HTTP localhost:8765            │
└──────────────────────┘   └───────────────────────────────┘
```

**依赖方向铁律**:核心层不认识插件层,插件层不认识桌面层。所有跨层通信走事件契约或协议接口,任何 sherpa-onnx / sounddevice / VoxCord 的类型都不得出现在公开事件结构里。

## 2. 事件契约(contract,契约)

`contracts/voice-events.schema.json` 是前后端唯一的通信约定,JSON Schema Draft 2020-12。

**信封结构**(envelope,信封):

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | 常量 `"1"` | 是 | 契约版本,破坏性变更时递增 |
| `type` | 枚举 | 是 | 9 种事件类型之一 |
| `id` | 字符串 | 是 | 事件唯一标识(UUID 或 `state-N`) |
| `timestamp` | ISO 8601 | 是 | UTC 时间戳 |
| `payload` | 对象 | 否 | 各事件类型自有字段 |

`additionalProperties: false` — 严禁额外字段,这是防止 SDK 类型泄漏的第一道闸门。

**9 种事件类型**:

| 事件 | 触发时机 | payload 关键字段 |
|---|---|---|
| `wake.detected` | 唤醒词命中 | `keyword`, `score`, `synthetic?` |
| `wake.rejected` | 唤醒被否决(未实现) | — |
| `turn.started` | 回合开始 | `text` |
| `asr.final` | 识别文本定稿 | `text` |
| `llm.delta` | 回复增量 | `text` |
| `tts.chunk` | 合成音频分块 | `index`, `text` |
| `turn.done` | 回合正常结束 | — |
| `turn.cancelled` | 回合被打断 | — |
| `state.changed` | 状态迁移 | `from`, `to`, `reason` |

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

## 4. 核心层:语音提供器(`core/providers.py`,295 行)

四个类,**全部懒加载**(lazy loading,延迟加载):模型缺失、依赖未安装、设备不可用时返回诊断结果,**绝不在模块导入阶段崩溃**。这是让项目在无模型环境下仍可测试的关键。

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

- 动态从 `D:\program\voxcord`(可用 `EVOX_VOXCORD_ROOT` 覆盖)导入唤醒与 VAD 实现。
- 本机无此目录,`load()` 返回 `available: False, reason: "voxcord core not found"`。
- 相关测试已用 `pytest.mark.skipif` 优雅跳过,**不影响发布路径**。

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

## 6. 插件门面层(`evox_plugin/plugin.py`,198 行)

`VoicePlugin` 是 EvoX 看到的唯一接口,dataclass 实现,持有状态机、事件列表、可选传输层与可选采集层。

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
| `visible` | 由 `EVOX_WAKE_VISIBLE` 环境变量控制 | 默认隐藏,便于无干扰开发 |
| `resizable` | `false` | 固定尺寸 |

定位逻辑:读当前显示器尺寸与 `scale_factor`,水平居中、底部上移 250 逻辑像素。**注意**:该 DPI 换算只做过 `cargo check`,真机 125%/150%/175% 缩放未验收(发布阻塞项 #5)。

### 7.2 前端(`desktop/src/main.ts` 34 行 + `style.css`)

- 监听 `evox-voice-state` 自定义事件,取 `state` 与 `amplitude` 驱动渲染。
- 状态中文标签:待机 / 已唤醒 / 聆听中 / 思考中 / 正在回复 / 需要处理。
- `amplitude` 钳制在 `[0.12, 1]`,写入 CSS 变量 `--amplitude`;`--phase` 由 `requestAnimationFrame` 循环推进。
- 当前是 **CSS-only 渲染**(conic-gradient 核心 + 三层波纹环 + blur)。Canvas 2D 生产渲染器属 Phase 4(t28–t35),**尚未实现**。
- 已内置自验证钩子:`__READY__`、`render_state_to_text()`、`step(ms)`,以及 `?test=1` 冻结模式。
- CSP 收紧为 `default-src 'self'; style-src 'self' 'unsafe-inline'`。

## 8. 组件边界速查表

| 边界 | 谁不许知道谁 | 强制手段 |
|---|---|---|
| 模型推理 ↔ 音频设备 | `SherpaKeywordProvider` 不碰麦克风 | 采集逻辑独立在 `SounddeviceWakeCapture` |
| 核心层 ↔ 插件层 | `core/` 不 import `evox_plugin/` | 单向依赖,插件层 import 核心层 |
| 后端 ↔ 前端 | 前端只认事件契约 | JSON Schema + `additionalProperties: false` |
| 业务 ↔ 会话后端 | 插件只认 `ConversationTransport` | Protocol 接口,mock 可完全替代 |
| 项目 ↔ VoxCord | 无 VoxCord 也能跑全部发布路径测试 | 动态 import + skipif 门控 |
| 渲染路线 ↔ 应用协调 | 换 Canvas/WebGL 不动业务代码 | 计划中的 `Renderer` 接口(Phase 4) |

## 9. 尚未实现的架构件(Phase 4 起)

| 组件 | 计划位置 | 说明 |
|---|---|---|
| 流式 ASR 提供器 | `core/providers.py` | 目前只有 KWS,识别文本靠外部注入 |
| TTS 播放队列与打断 | 新增模块 | 现只发 `tts.chunk` 事件,无真实播放 |
| Canvas 2D 生产渲染器 | `desktop/src/renderer/` | t28–t35,含质量分档选择器 |
| `Renderer` 抽象接口 | 同上 | 让 WebGL v2 可替换 Canvas 实现 |
| 超时/重连/错误恢复 | `core/state.py` + 传输层 | 生产化状态机 |
| 系统托盘常驻 | `main.rs` | 发布阻塞项 #5 一部分 |
| `wake.rejected` 事件产出 | 插件层 | 契约已定义,尚无产出点 |
