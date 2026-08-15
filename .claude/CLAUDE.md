# EvoX Voice Wake — 项目规则

全局准则见 `~/.claude/CLAUDE.md`。本文件只写本项目特有的约束。

## 三条设计红线(不得违反)

1. **本地优先** — 唤醒、VAD（语音活动检测）、TTS（语音合成）、声纹校验全部本机执行；项目代码不保存、不上传音频。新增依赖若含云调用或 telemetry（遥测），直接否决。声纹环形缓冲永不落盘；记忆库只存文本、永不存音频。
2. **组件可替换** — KWS/ASR/TTS/会话传输/**agent 后端**都在契约之后。任何 `sherpa-onnx`、`sounddevice`、VoxCord 的类型都不得出现在 `contracts/voice-events.schema.json` 或公开事件结构里（`additionalProperties: false` 是第一道闸门）。`AgentDescriptor`/`Task`/`AgentChunk` 的字段只许 `str`/`int`/`float`/`frozenset`/`tuple`/`Mapping`。
3. **验证等级诚实** — 七级证据 DOC < AUTO < SIM < REAL-MIC < **REAL-AGENT** < REAL-EVOX < REAL-WIN。禁止把低等级当高等级用，禁止把 mock（模拟）验证写成真机验收。**mock 子进程只算 SIM，不算 REAL-AGENT**。声明结论必须标注等级。

## 改完什么跑什么

完整例程见 `docs/routines.md`。最小回归对照表：

| 改动范围 | 命令 | 期望 |
|---|---|---|
| `core/` `evox_plugin/` | `.venv\Scripts\python.exe -m pytest tests -q` | **588 passed, 2 skipped** |
| `contracts/voice-events.schema.json` 或事件结构 | `pytest tests/test_event_schema.py tests/test_events.py tests/test_voice_contract.py tests/test_plugin_tools.py -q` | 全绿 |
| `contracts/agent-events.schema.json` `agents.schema.json` | `pytest tests/test_agent_event_schema.py -q` | **34 passed** |
| `core/events.py` | `pytest tests/test_events.py tests/test_agent_event_schema.py -q` | 全绿 |
| `core/audio/`(除 speaker) | `pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q` | 全绿 |
| `core/audio/speaker.py` `ring.py` `capture.py` | `pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | **30 passed**（**不需要声纹模型**） |
| 声纹阈值或判别力 | `pytest tests/integration/test_speaker_model.py -q` | 5 passed（缺模型时 5 skipped） |
| TTS 合成（需模型） | `pytest tests/integration/test_tts_model.py -q` | 4 passed（缺模型时 2 skipped） |
| `core/tools/` | `pytest tests/test_tools.py tests/test_tool_security.py -q` | **123 passed, 1 skipped**（skip 是符号链接越界，本账户无权建链接） |
| `core/memory/` | `pytest tests/test_memory.py -q` | **63 passed** |
| 工具/记忆与语音路径接线 | `pytest tests/test_memory.py tests/test_plugin_tools.py -q` | **87 passed** |
| `core/agents/` `config/agents.toml` | `pytest tests/test_agent_contract.py tests/test_agent_cli.py tests/test_agent_evox.py tests/test_agent_acp.py tests/test_agent_http.py -q` | 全绿（contract 14 + cli 28 + evox 17 + acp 10 + http 9） |
| `core/dispatch/` | `pytest tests/test_router.py tests/test_dispatcher.py tests/test_aggregator.py tests/test_intent.py tests/test_breaker.py -q` | **159 passed**（router 30 + dispatcher 37 + aggregator 20 + intent 54 + breaker 18） |
| `core/session_bridge.py` | `pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q` | 全绿 |
| `desktop/src/` | `cd desktop && npm run build` | tsc + vite 通过 |
| `desktop/src-tauri/` | `cd desktop/src-tauri && cargo check && cargo test` | 零警告 + **15 passed** + **须实机验收** |

必须用隔离环境的 `.venv\Scripts\python.exe`，不用系统 Python（系统环境没装 sherpa-onnx / soundfile）。

skip 数会随环境变化（VoxCord、模型是否存在），**passed 数下降才是回归**。

## 当前阶段

Phase 4（生产实现）**进行中**：P0 骨架、P1 声纹门、P2 平台事件契约、P3 记忆系统、P4 本地工具与安全门、P5 agent 适配器、P6 派发/路由/汇总、P7 ACP/HTTP 适配器已落地（P8 唤醒球 UI 也已到 AUTO+SIM 级），「Python→桌面事件通道」已接线到代码级（cargo test 15 passed、npm build 通过），下一步 P9/P10 真机验收。十阶段划分与 11 项发布阻塞项见 `docs/project-overview.md` 第 5、6 节。

决策记录：ADR 001 语音栈 · ADR 002 声纹准入 · ADR 003 agent 接入 · ADR 004 记忆 · ADR 005 派发与工具门。

**未实现，不要假设存在**：
- `web.search` 的**真实后端** —— 平台不自带（每个托管搜索 API 都是带 key 的云依赖），未注入时工具报 `no search backend is configured`
- 流式 ASR（识别文本靠外部注入）、TTS 多段排队（打断已通：`wake_detected` 在 SPEAKING/THINKING 先 `cancel()` 停 TTS + transport 再进 LISTENING，`attach_capture` 把 capture 的 `on_wake`/`on_reject` 指到插件；真机说话打断是 REAL，需麦克风+扬声器在场）
- Canvas 2D 生产渲染器（现在是 DOM + CSS，六态与动画都已落地）
- 超时/重连/错误恢复（系统托盘已由 `build_tray` 实现：显示/隐藏/退出，Rust 侧建、不扩 IPC 面；真机点开未验）
- 声纹**反欺骗**：门不防录音回放，这是已知缺口（ADR 002 局限），不是待办
- 声纹**真机验收**：本人通过率、他人拒绝、回放实测都需你在场（P10）
- 记忆**跨进程持久性**：Markdown 往返在同进程内已验，重启后仍未验（P10）
- **真实外部 agent 跑通一轮** —— `cli.py` 的全部测试用 mock 子进程，只算 SIM；真跑 `claude -p` 是 P9（REAL-AGENT）

**已完成但容易记错的**：
- **语音接进派发的是 `VoiceRuntime`，不是 `VoicePlugin`** —— `VoicePlugin.submit_text` 本身不走派发（门面只做状态机 + 记忆 + transport，不自造已验说话人）。`evox_plugin/runtime.py` 的 `VoiceRuntime.say()` 构造 `Dispatcher`，`submit_text` 之后 `dispatcher.dispatch()`，再 `complete_turn` 说回答案。「说一句 → 读文件」的链路已接上并有 `tests/test_runtime.py` 覆盖；`VoicePlugin.run_tool()` 仍是 opt-in 的手动入口。`_reach_listening()` 是实例方法、操作 `self.plugin`（此前是 `@staticmethod` 在操作一个丢弃的副本）
- **P7 `acp` + `http` 适配器已实现** —— `acp.py` 讲 JSON-RPC 2.0 over stdio（initialize → session/new → session/prompt，`session/update` 流式增量）；`http.py` 讲 OpenAI Chat Completions（SSE 流式 + 非流式回退）。http 的 token 只从 `EVOX_AGENT_HTTP_TOKEN` 读，url 遵循桥接同款约束（明文 HTTP 只许回环、带凭据拒绝）。都算 SIM（mock peer / mock server），真实联调是 P9 的 REAL-AGENT
- **记忆召回已接进派发、短期层自裁剪** —— `Dispatcher._recall_context()` 只在 agent 路径上把 `facts()` + `recent_turns()` 的文本拼进 `Task.context`（工具路径不召回，快路径保持快）；召回失败静默吞掉（记忆是增强不是前提）。`write_turn()` 每次接受写入后 `prune_turns()` 自裁剪（`short_keep=200`）。runtime 把 `session_id` 传进 `open_memory`，故 `recent_turns(session_id=...)` 能按会话匹配
- **TTS 合成+播放已接线** —— `core/audio/playback.py` 的 `SounddevicePlayback`（sounddevice 懒加载）；`SherpaTtsProvider.speak()` = `synthesize()` + 播放（`playback` 可注入 fake，测试用）；`VoicePlugin.attach_tts()` opt-in，`complete_turn` 在 SPEAKING 与 turn.done 之间 `speak(reply)`（失败吞掉，不结束回合），`cancel()` 调 `stop()`。事件序列不变（`speak` 是副作用）。真实出声是 REAL，需扬声器在场
- **唤醒词打断已接线** —— `wake_detected` 在 THINKING/SPEAKING 时先 `cancel()`（停 TTS + 停 transport、发 `turn.cancelled`）再进 LISTENING，返回的仍是 `[wake.detected, state.changed]`；`attach_capture` 把 capture 的 `on_wake`/`on_reject` 指到插件，capture 的 InputStream 在 speaking 期间不关，所以「说唤醒词打断正在播的回复」这条链在代码级是通的。真机打断是 REAL
- 声纹门已接线：3 秒内存环形缓冲 + KWS 命中即校验 + `require_verification` 默认 `True` + `wake.rejected` 产出点
- 37.8 MB 声纹模型**已下载**（dim 512，SHA-256 记在 `THIRD_PARTY_NOTICES.md`）
- `feed()` 返回 `(keyword, None)` —— sherpa-onnx 的 `KeywordResult` **根本不含置信度**，`None` 是经核实的陈述不是遗漏；`wake.detected` 的分数来自声纹相似度
- 两个事件契约共用同一信封：语音 9 种 + 平台 12 种，`type` 枚举互斥。`validate_any_event()` 按 `type` 查表选契约，`validate_event(event, path)` 仍是严格路径
- 记忆已接进语音路径，但是**opt-in**：`plugin.attach_memory(writer, recaller)` 不调用就没有数据库文件；`submit_text` 写用户轮、`complete_turn` 写助手轮，写入失败被静默吞掉（记忆不是对话的前提条件）
- 工具同样是 **opt-in**：`plugin.attach_tools(open_tools(...))` 不调用则 `run_tool()` 直接返回 `tools are not attached`
- **`shell.run` 的 `confirmed` 必须是 `is True`**，不是「真值」—— `"confirmed": "no"` 是个真值字符串，按真值判断会直接执行。这是测试抓出来的
- **危险模式不可配置**：13 条 `DANGEROUS_PATTERNS` 在 `policy.py` 代码里，配置文件里写 `dangerous_patterns` 会报 `unknown config key`
- **`tool.confirm_required` 是唯一带命令原文的事件** —— 唤醒球必须显示它将要运行什么（FR-6.13）；其余 `tool.*` 事件只带决定、原因、耗时
- **FTS5 默认分词器搜不到中文**（已实测）。索引的是派生 token 列：索引侧 = 单字 + 相邻双字，查询侧 = 只用双字。改分词必须两侧同时改，见 ADR 004 的 2026-08-02 修正
- **`config/agents.toml` 已落地**（86 行，`claude` 默认开、`codex`/`opencode`/`evox` 默认关）。它**没有放密钥的键**：schema 层面就不存在，写 `token = "..."` 直接校验失败
- **agent 的失败是 chunk 不是异常** —— 命令不在 PATH、非零退出、超时、取消四条路径都以带 `error` 的终结 `done` chunk 到达。派发器因此不必给每条流包 `try`
- **每条 agent 流恰好一个 `done`** —— agent 自报的 `done` 被折进终结 chunk 而不是转发。这是判断回合结束的唯一依据
- **放弃 agent 流即杀进程** —— 生成器的 `finally` 收尸，`race` 丢弃输家时不留野进程
- **`evox` 适配器不流式，也不可能流式** —— 桥接是一次阻塞 POST，没有增量端点；首字延迟 = 整轮延迟是端点的属性，不要给它套「看起来增量」的外壳
- **命令不在 PATH 的 agent 条目被保留而不是丢弃** —— 可用性由 `check()` 报告，丢掉它会让「少一个 agent」与「配错一个 agent」无法区分
- **五个 sink 的签名统一为 `on_event(event)` 单个已验证信封** —— tool runner、记忆写、记忆召回、dispatcher、breaker 都是这一个形状。之前 breaker 是三参 `(type, agent, detail)`，测试按位置解包，改签名不会报错只会静默错位
- **没有 `task.completed`，只有 `task.done`** —— 12 种平台事件的枚举是钉死的，拼错的类型名在 `validate_event()` 处就炸
- **`task.progress` 报的是派发集合 `agents`（复数），不是获胜者** —— `AgentChunk` 没有 `agent` 字段，合并后的 chunk 不带来源。想报「谁答的」就得先给 chunk 加来源字段，猜不出来
- **`race` 的获胜者在首个 chunk 时决定，不是完成时** —— 按完成判定会让**空流**赢（它最先「完成」）。带 `error` 的 `done` 也算「说话了」：不静默切给输家，否则合并流就变成了重试机制，而 dispatcher 会把失败的一轮记成成功
- **`fanout` 只折叠 `done`，其余 chunk 原样转发** —— 合并后的 `done`：elapsed 取最慢、tokens 求和、**只有全部 agent 都失败**才带 error。tokens 全无上报时是 `None` 不是 `0`（`0` 会被读成「数过，是零」）
- **能力（capability）是 gate 不是 weight** —— 缺 vision 的 agent 得 0.0 并被移出计划，不会被「便宜又快」抬进来。5 维评分的权重和为 1.0，未知成功率取 `UNKNOWN_SUCCESS=0.5`，越界的 cost/latency 被 clamp 而不是外推
- **意图的动词必须带边界（`_VERB_SEP`）** —— 只锚定开头不够：「运行时报错了怎么办」以「运行」开头，会把「时报错了怎么办」当 shell 命令跑；「搜索引擎是怎么工作的」同形。代价是粘着写法（「查看README.md」）落到 agent —— 这是安全方向
- **`needs_confirmation` 被原样带出，dispatcher 永不自动确认** —— 有测试钉死「恰好调用一次，绝不带 `confirmed=True` 重试」
- **唤醒球的命中区由前端量、Rust 判**。`set_ignore_cursor_events` 是**整窗开关**，没有 Electron 的 `forward` 选项，所以选择性穿透只能靠 Rust 侧 30ms 轮询光标。圆心与半径**不许写进 Rust**：它们是 CSS 布局的结果，硬编码会在下次改样式时静默漂掉
- **`measureHitRegion()` 用 `offsetLeft`/`offsetWidth`，不用 `getBoundingClientRect()`** —— 球每帧被 rAF 写 `transform`，用渲染盒会让命中区每帧抖动、IPC 每帧都发
- **命中判定的失败路径一律倒向「窗口吃鼠标」** —— 读不到光标/窗口位置/缩放、或前端还没上报，都当命中。反方向会让确认卡变成一张点不动的图，而点不动的确认等价于没有确认
- **没有 `capabilities/` 文件，这是故意的** —— Tauri 2 只对 `plugin:` 前缀命令或应用自带 ACL manifest 时才查权限（`tauri-2.10.3` `webview/mod.rs:1802`）。不放反而最紧：所有 `core:*` 插件命令对前端不可达，IPC 面就是四个 `evox_*`（report_layout / start_drag / set_visible / confirm_reply）。代价是前端**永不能 import `@tauri-apps/api`**，只用 `__TAURI_INTERNALS__.invoke`
- **Python→桌面事件通道已接线（代码级）** —— `core/desktop_bridge.py` 走父进程管道发 `{"kind":"event"|"visible"}`、收 `{"kind":"ready"|"confirm"}`；Rust 侧 `spawn_event_reader` 读 stdin 把整行原样投成 `evox-bridge` CustomEvent（`js_string_literal` 转义防注入，有测试），`evox_confirm_reply` 把确认写回 stdout；前端 `applyEnvelope` 分派 `state.changed`/`turn.*`/`llm.delta`/`task.failed`/`tool.*`，`askConfirm`→`evox_confirm_reply`。cargo test **15 passed**、npm build 通过；**真机窗口上的点击/焦点/Esc 仍未验（P10 REAL-WIN）**
- **系统托盘已实现（`build_tray`）** —— 球是无边框 + skip_taskbar + 置顶，桌面上没有别的入口能关它，托盘是用户唯一的「显示/隐藏/退出」路径。托盘是 Rust 侧直接建的（`Menu` + `TrayIconBuilder` + `on_menu_event`），和四个 `evox_*` 命令无关，**不扩大 IPC 面**。隐藏时若挂着确认卡，Python 侧 `await_confirmation` 60s 超时落定为拒绝（fail-closed）。真机点开托盘菜单未验（REAL-WIN）
- **`.shadow(false)` 不是可选项** —— 无边框 + 透明还留投影的话，桌面上会有一块跟着球走的方形灰影
- **拖动是自己的 `evox_start_drag` + 4px 阈值**，不是 `data-tauri-drag-region`（那条路要 `core:window:allow-start-dragging`，等于为拖窗口暴露整个 core:window）。拖完 OS 补的 `click` 只在 `detail > 0` 时吞 —— 键盘激活的 `click` 的 `detail` 是 0，连它一起吞会让球没法用键盘按
- **`evox_set_visible(false)` 会先把挂起的确认按拒绝落定再隐藏** —— 隐藏一张挂起的确认卡等于让调用方永久挂起，而「挂起」在安全语义上等价于「未拒绝」

## 注意事项

- **已是 git 仓库**（基线 `9f7d923`）。改动前 `git status --short` 确认工作区，不要覆盖无关脏文件。破坏性 git 操作（`reset --hard`、`push --force`、`clean -f`）一律先问。
- **`VoiceState` 六态不改**，`contracts/voice-events.schema.json` **字节不变**、version 保持 `"1"` —— 这条现由 `tests/test_agent_event_schema.py` 的 SHA-256 摘要钉死，不靠自觉。平台事件走 `contracts/agent-events.schema.json`。
- **信封只在 `core/events.py` 构造**。新事件类型加进契约文件即可，Python 侧不需要同步改。
- **`enrollment/` 是生物特征**，已在 `.gitignore` 内，永不提交；查看注册状态只用 `describe()`，绝不输出向量。
- **`memory/` 是个人数据**，已在 `.gitignore` 内（要不要纳入版本控制由你决定，取消那一行即可）。记忆事件只带 id / 计数 / 标签，**永不带文本** —— 事件会扇出到每一个日志与传输通道。凭据形状的文本**整条拒绝而不是打码**：多行私钥正是打码会留下正文的那个例子。
- **声纹 fail-closed 断言不许绕过**：模型缺失、无人注册、校验抛异常都必须落在拒绝一侧。一个静默放行的声纹门比没有门更糟。
- **`shell.run` 默认关闭**，白名单外的命令**拒绝而非询问**（询问会训练出无脑点确认的习惯）。危险模式在代码里、不在配置里：配置的白名单只能收窄，永远不能放宽。
- **`config/tools.toml` 里写错的键会报错而不是被忽略** —— 拼错 `denied_names` 会静默扩大沙箱，一个「看起来在约束什么但其实没有」的配置比两个极端都糟。
- 文档要同步更新：实测数据进 `docs/research/prototype-results.md`，新例程进 `docs/routines.md`，依赖与模型版本进 `THIRD_PARTY_NOTICES.md`。
- 控制台中文乱码是 Windows 代码页显示问题，UTF-8 字节正确，**不是缺陷，不要去「修」**。
- `models/` 约 451 MB（含 37.8 MB 声纹模型），其中 `kws.tar.bz2` + `tts.tar.bz2` 共 192 MB 是可删归档。不要把模型文件当代码改动处理。
- 桥接安全姿态已加固（bearer token 强制、loopback 校验、URL 凭据拦截、turn_id 编码），改 `core/session_bridge.py` 时不得降级这些校验；它被包装成 `agents/evox.py` 后同样不得降级。
- `github.com` / `api.github.com` / `raw.githubusercontent.com` 的 WebFetch 在本环境被拦截，无法读一手 README。开源项目判定只能标「社区来源」，不得当官方确认用。
