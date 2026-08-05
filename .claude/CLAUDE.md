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
| `core/` `evox_plugin/` | `.venv\Scripts\python.exe -m pytest tests -q` | **359 passed, 3 skipped** |
| `contracts/voice-events.schema.json` 或事件结构 | `pytest tests/test_event_schema.py tests/test_events.py tests/test_voice_contract.py tests/test_plugin_tools.py -q` | 全绿 |
| `contracts/agent-events.schema.json` `agents.schema.json` | `pytest tests/test_agent_event_schema.py -q` | **34 passed** |
| `core/events.py` | `pytest tests/test_events.py tests/test_agent_event_schema.py -q` | 全绿 |
| `core/audio/`(除 speaker) | `pytest tests/test_provider_adapter.py tests/test_sherpa_provider.py -q` | 全绿 |
| `core/audio/speaker.py` `ring.py` `capture.py` | `pytest tests/test_speaker.py tests/test_speaker_privacy.py -q` | **30 passed**（**不需要声纹模型**） |
| 声纹阈值或判别力 | `pytest tests/integration/test_speaker_model.py -q` | 5 passed（缺模型时 5 skipped） |
| `core/tools/` | `pytest tests/test_tools.py tests/test_tool_security.py -q` | **123 passed, 1 skipped**（skip 是符号链接越界，本账户无权建链接） |
| `core/memory/` | `pytest tests/test_memory.py -q` | **62 passed** |
| 工具/记忆与语音路径接线 | `pytest tests/test_memory.py tests/test_plugin_tools.py -q` | **86 passed** |
| `core/agents/` `config/agents.toml` | `pytest tests/test_agent_contract.py tests/test_agent_cli.py tests/test_agent_evox.py -q` | **59 passed**（contract 14 + cli 28 + evox 17） |
| `core/dispatch/` | `pytest tests/test_router.py tests/test_dispatcher.py -q` | 全绿（P6 起） |
| `core/session_bridge.py` | `pytest tests/test_session_bridge.py tests/test_plugin_tools.py -q` | 全绿 |
| `desktop/src/` | `cd desktop && npm run build` | tsc + vite 通过 |
| `desktop/src-tauri/` | `cd desktop/src-tauri && cargo check` | 零警告 + **须实机验收** |

必须用隔离环境的 `.venv\Scripts\python.exe`，不用系统 Python（系统环境没装 sherpa-onnx / soundfile）。

skip 数会随环境变化（VoxCord、模型是否存在），**passed 数下降才是回归**。

## 当前阶段

Phase 4（生产实现）**进行中**：P0 骨架、P1 声纹门、P2 平台事件契约、P3 记忆系统、P4 本地工具与安全门、P5 agent 适配器已落地，下一步 P6（派发/路由/汇总）。十阶段划分与 11 项发布阻塞项见 `docs/project-overview.md` 第 5、6 节。

决策记录：ADR 001 语音栈 · ADR 002 声纹准入 · ADR 003 agent 接入 · ADR 004 记忆 · ADR 005 派发与工具门。

**未实现，不要假设存在**：
- 平台层的 `dispatch` 包 —— 现在只有 `contract.py`（93 行 Protocol），`dispatcher`/`router`/`aggregator`/`intent`/`breaker` 全在 P6
- `acp` / `http` 两种 agent kind —— 契约里有枚举，适配器在 P7；配置里 `enabled = true` 会带阶段名报错
- 平台 12 种事件里除 `memory.*` / `tool.*` 之外的**产出点** —— `task.*`/`agent.*` 在 P6
- **意图识别与工具的自动调用** —— 门和三个工具都在，但没有任何东西会自己决定去读文件或搜索；「读一下 X」→ 直执行是 dispatcher 的职责（P6）。当前唯一入口是 `plugin.run_tool()`
- **agent 与语音路径的接线** —— `open_agents()` 能造出适配器，但插件还不会派发给它们，那是 P6
- `web.search` 的**真实后端** —— 平台不自带（每个托管搜索 API 都是带 key 的云依赖），未注入时工具报 `no search backend is configured`
- `shell.run` 的**确认 UI** —— 门会返回 `needs_confirmation`，但唤醒球上的展示与确认动作是 P8（REAL-WIN）
- 记忆**召回的消费者** —— `MemoryRecaller` 已能查，但把结果注入任务是 dispatcher 的职责（P6）；`prune_turns()` 也还没有调用者，短期层暂不自动裁剪
- 流式 ASR（识别文本靠外部注入）、TTS 播放队列与真实打断
- 唤醒球运行时显隐（可见性现由 `EVOX_WAKE_VISIBLE` 静态决定）、Canvas 2D 生产渲染器、工具确认 UI
- 超时/重连/错误恢复、系统托盘
- 声纹**反欺骗**：门不防录音回放，这是已知缺口（ADR 002 局限），不是待办
- 声纹**真机验收**：本人通过率、他人拒绝、回放实测都需你在场（P10）
- 记忆**跨进程持久性**：Markdown 往返在同进程内已验，重启后仍未验（P10）
- **真实外部 agent 跑通一轮** —— `cli.py` 的全部测试用 mock 子进程，只算 SIM；真跑 `claude -p` 是 P9（REAL-AGENT）

**已完成但容易记错的**：
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
