# 交接文档 —— Vox

> 交接时间：2026-08-29  ·  最近完成：语音交互闭环（自定义唤醒词 + 唤醒确认音 + 本机工具 + 运行日志）、ASR 模型换代、TTS 中文前端修复
> 验证基线：Python **1190 passed, 3 skipped** · DesktopBridge 专项 **33 passed** · Rust **15 passed** · `npm run build` 通过
> 基线在**干净 shell**（未设置 `PYTHONUTF8` / `PYTHONIOENCODING`）下复现；数字取决于环境变量的话就不是基线。
> 本文件是**接手者的第一份材料**。项目全貌看 [`docs/project-overview.md`](project-overview.md)，
> 干活的规矩看 [`.claude/CLAUDE.md`](../.claude/CLAUDE.md)（那份是硬约束，本文件只做导航），
> 故意没做的技术债看 [`docs/backlog.md`](backlog.md)。

## 接手第一件事：起控制台

```powershell
.\.venv\Scripts\python.exe scripts/run_console.py
```

浏览器打开一个带 token 的回环地址。**就绪清单**逐项告诉你还缺什么、怎么补；声纹可以
直接在页面上录 3 段完成注册。这一页取代了此前那份十几条命令的安装清单。

## 事件 sink 的既有姿态

派发、熔断和记忆的事件 sink 是 **best-effort 旁路**：sink 抛异常只被计数，不会中断
正在进行的回合。这是刻意的 —— 一个日志通道挂掉不该让对话挂掉。相关计数由各自的
`sink_failures` 暴露。


## 1. 这个项目是什么

Windows 上的**开放式语音唤醒对话平台**。目标链路：

```text
说出唤醒词 → 声纹确认是本人 → 唤醒球弹出 → 本地识别语音
    ├─ 简单的事：平台自己做（读文件 / 搜索 / 终端 / 记忆），毫秒级
    └─ 复杂的事：派发给 claude / codex / opencode / evox，路由汇总
→ 流式回复 → TTS 朗读 → 继续多轮 → 说唤醒词随时打断
```

三条设计红线（**任何改动都不许破**，细则见 CLAUDE.md）：

1. **本地优先** —— 唤醒 / VAD / ASR / TTS / 声纹全部本机执行，项目代码不存不传音频。
2. **组件可替换** —— KWS/ASR/TTS/传输/agent 后端都在契约之后，第三方 SDK 类型不得泄漏进事件。
3. **验证等级诚实** —— DOC < AUTO < SIM < REAL-MIC < REAL-AGENT < REAL-EVOX < REAL-WIN，**mock 不算真机**。

## 2. 现在能跑到哪一步（诚实版）

语音五段在**代码级全部打通，各自经真实模型验证过，而且现在有一条命令能把它们串起来**；
但**没有一次是在你的麦克风+扬声器前跑完整轮的**。

| 环节 | 实现 | 已验证到 | 缺什么 |
|---|---|---|---|
| 唤醒（KWS） | `core/audio/kws.py` | 真机单次唤醒（2026-07-26）+ 合成音触发 | 多场景统计 |
| 声纹门 | `core/audio/speaker.py` | AUTO：判别力 0.736/0.370、阈值 0.5 落间隙 | 本人通过率 / 他人拒绝 / 回放 |
| **声纹身份→授权** | `capture.on_verified` → `plugin.verified_speaker` → `runtime.effective_speaker` | AUTO 15 例（fake verifier） | 真机（此前是一个字符串常量，见 §5） |
| 流式 ASR | `core/audio/asr.py` | 真实模型转写 bundled wav 成功 | 真机麦克风转写 |
| 派发 / 路由 / 汇总 | `core/dispatch/` | AUTO+SIM 159 用例 | 真实 agent 跑一轮（见下） |
| TTS 合成 + 播放 | `core/audio/tts.py` `playback.py` | 真实模型合成 44.1kHz / 243ms（控制台可复现） | 扬声器出声 |
| 打断（barge-in） | `plugin.wake_detected` + `cancel` | AUTO（fake TTS） | 真机说话打断 |
| **语音栈组装** | `vox_plugin/voice_stack.py` | AUTO 16 例（缺模型的降级路径全覆盖） | — |
| **本机控制台** | `core/console/` + 单文件前端 | AUTO 82 例 + SIM 渲染取证 | 浏览器真实录音授权（REAL-MIC） |
| **MCP 工具** | `core/tools/mcp.py` | SIM 51 例（进程内假 server） | 真实 MCP server 一次 `tools/call` |
| **搜索后端** | `core/tools/search_backends.py` | AUTO 35 例（不打真网络） | 真实检索一次 |
| 唤醒球 / 托盘 / 事件通道 | `desktop/` + `core/desktop_bridge.py` | DesktopBridge 33 + cargo 15 + npm build | 透明窗口 / DPI / 点击 |
| 记忆 | `core/memory/` | 双进程 + **多线程 7 例**（本轮修了一个真缺陷） | 应用重启的人工确认 |

**REAL-AGENT 为什么还欠着（2026-08-24 实测）**：`claude` CLI 在 PATH（2.1.223），但**从子进程调起时返回 `Not logged in · Please run /login`（exit 1）** —— 嵌套调用拿不到当前会话的凭据。`codex exec` 90s 无输出，`opencode` 连不上云端点。这条不是「还没试」，是**试过且被挡住**。恢复任一后端后跑 `scripts/acceptance/probe_agents.py`，它报三个互不混淆的等级（configured / available / REAL-AGENT），**一条干净但没有文字的流不算答上**。

**一句话**：代码是齐的，装起来也不难了（控制台会告诉你缺什么），**REAL 级证据仍然是缺的**。


## 3. 代码地图（92 个受版本控制的代码文件，按「改这里要看什么」组织）

```text
core/
  audio/        设备与模型：kws 唤醒 · asr 流式识别 · tts 合成 · playback 播放
                capture 采集(两段模式 + on_verified) · speaker 声纹 · ring 环形缓冲 · vad
                config.py 语音栈配置（参数进 TOML，模型路径走环境变量）
  console/      ★ 本机控制台：server(回环+token) · routes(API 与白名单) · audio(WAV 解析)
                static/index.html(单文件前端，CSS/JS 内联)
  agents/       agent 适配器：cli · acp · http · evox + registry/schema/contract
  dispatch/     intent 意图 · router 五维路由 · aggregator 汇总 · breaker 熔断 · dispatcher
  memory/       store(SQLite+FTS5，线程安全) · write · recall · contract
  tools/        policy 安全门(含 mcp 分支) · fs · web · shell · runner
                search_backends.py(SearxNG/DDG，都默认关) · mcp.py(远端工具，三层默认关)
  config_edit.py     ★ TOML 行级写入器（保注释、只改已存在的键、校验后原子替换）
  events.py     ★ 事件信封唯一构造点
  state.py      ★ 六态状态机（不许改）
  desktop_bridge.py  Python↔唤醒球的管道通道
vox_plugin/
  plugin.py     VoicePlugin 门面（状态机 + 记忆 + 工具 + TTS + capture + verified_speaker）
  runtime.py    ★ VoiceRuntime：装配 + effective_speaker（麦克风模式只认门的答案）
  voice_stack.py ★ open_voice_stack：四个模型 + 一个 capture，缺声纹不降级
contracts/      voice-events(9 种，字节不变) · agent-events(12 种) · agents · mcp
desktop/        Tauri 2 唤醒球：src/ 前端 · src-tauri/src/main.rs 窗口+托盘+事件通道
config/         voice · speaker · tools · agents · memory · mcp（**六份，无密钥键**）
scripts/        run_console(控制台) · run_voice(说话) · run_desktop(打字)
                acceptance/ 真机验收：live_wake · live_conversation · probe_agents
                            resource_profile · smoke_microphone · verify_memory_persistence
                enroll_speaker.py 命令行声纹录入（控制台是它的界面版）
```

**读代码的建议顺序**：`core/state.py`（6 态）→ `core/events.py`（信封）→ `vox_plugin/plugin.py`（门面）
→ `vox_plugin/runtime.py`（装配，看 `say()`、`pump()` 和 `effective_speaker`）→
`vox_plugin/voice_stack.py`（模型组装）→ `core/console/routes.py`（看三份白名单）→
再按需要下钻到 `core/dispatch/` 或 `core/audio/`。


## 4. 接手后第一件事：真机验收（需要你在场）

按这个顺序跑，每一步都**看返回值而不是「没报错」**：

```powershell
# 0) 环境自检（应当 1009 passed, 3 skipped）
#    先确认环境里没有 PYTHONUTF8 / PYTHONIOENCODING —— 基线只在干净 shell 里成立
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run

# 1) 起控制台，按就绪清单补齐（声纹可以直接在页面上录）
.\.venv\Scripts\python.exe scripts/run_console.py

# 2) 只测唤醒（说「你好问问」）。门默认开着，--no-gate 才只测 KWS
.\.venv\Scripts\python.exe scripts/acceptance/live_wake.py --all-hits --duration 60

# 3) 全链路：唤醒 → 说请求 → 派发 → 出声 → 再说唤醒词打断
.\.venv\Scripts\python.exe scripts/acceptance/live_conversation.py

# 4) REAL-AGENT 探测（任一后端登录后）
.\.venv\Scripts\python.exe scripts/acceptance/probe_agents.py

# 5) 30 分钟资源画像（可以挂着不管，但结论要你看环境写）
.\.venv\Scripts\python.exe scripts/acceptance/resource_profile.py --minutes 30

# 6) 唤醒球（先构建，再看透明/置顶/点击/托盘）
Push-Location desktop; npm run build; Pop-Location
Push-Location desktop/src-tauri; cargo build; Pop-Location
.\.venv\Scripts\python.exe scripts/run_voice.py
```

跑完把**实测数字**写进 [`docs/research/prototype-results.md`](research/prototype-results.md)，
并按真实等级更新 [`docs/project-overview.md`](project-overview.md) 第 6 节的 14 项发布阻塞项。
**不要**把跑通一次写成「已验证」——多场景统计才是。


## 5. 已知缺口（照实写，不要假设存在）

- ~~`web.search` 没有真实后端~~ **已实现，但出厂仍然关着（2026-08-28，AUTO）** ——
  `SearxBackend`（自建 SearxNG 回环，零 key 零云）优先，`DuckDuckGoBackend`（无 key HTML
  抓取，**是**对外请求）兜底。两个都默认关，不配就还是 `no search backend is configured`。
  坏的 `searx_url` **不 fallback 到外网**。真实检索一次是新的 REAL 项。
- ~~TTS 多段排队~~ **已解决（2026-08-24，AUTO）** —— `split_speech` 按句切分、`tts.chunk`
  逐段带 index、provider `speak_segments` 排队播放且 `stop()` 可中途弃队。
- ~~没有语音生产入口~~ **已解决（2026-08-28，AUTO）** —— `vox_plugin/voice_stack.py` 组装，
  `scripts/run_voice.py` 是入口，`scripts/run_console.py` 是「看缺什么并补」的入口。
  验收脚本改为复用同一装配，模型路径硬编码与 `speaker="owner"` 一并删掉。
- ~~声纹身份到不了授权判定~~ **已解决（2026-08-28，AUTO）** —— 此前 `capture._authorise()`
  拿到 `result.speaker` 只把 `score` 传出去，麦克风模式下 `shell.run` 的凭据是字符串常量。
  现在走 `on_verified` → `plugin.verified_speaker` → `runtime.effective_speaker`，**身份不进
  事件**，所有失败路径倒向 `None`。真机是 REAL-MIC。
- ~~记忆库多线程不可靠~~ **已解决（2026-08-28，AUTO）** —— 连接懒建绑在第一个查询的线程上，
  第二个线程抛 `sqlite3.ProgrammingError`。控制台是多线程的，所以症状是「保存档案成功、
  紧接着删除档案 sync 失败」，读起来像调用方的 bug。修法 `check_same_thread=False` + `RLock`
  （**必须可重入**：`write()` 会调 `connection`，两者都取锁）。7 例真线程测试。
- **真实 MCP server 未联调** —— 客户端已实现（stdio JSON-RPC，三层默认关，默认每次确认），
  但测试驱动的是进程内假 server，只算 **SIM**。
- **声纹反欺骗** —— 门不防录音回放，这是 ADR 002 明确记下的局限，**不是待办**。输入侧启发闸
  （静音/削波质量门、连续拒绝冷却、可选多窗口一致）抬高伪造成本但不构成反欺骗能力。
- **`scripts/fetch_models.py` 未实现** —— 形状与三条硬约束已成文
  （[model-distribution.md](model-distribution.md) §2.5），代码没写。
- **子进程输出编码只修了一半** —— Windows 上子进程默认按 ANSI 代码页编 stdout，父进程按
  UTF-8 读且 `errors="replace"`，于是乱码变成夹在正常回复里的 U+FFFD：**静默错，不报错**。
  `acp.py` 与 `mcp.py` 已给 Python 子进程注入 `PYTHONUTF8`/`PYTHONIOENCODING`（两个协议都
  规定 UTF-8，所以强制是对的）；**非 Python 的子进程无解**。`claude` 实测不受影响（Node 恒写 UTF-8）。
- **`VoxCordAdapter` 的 sys.path 与 voxcord 布局不匹配** —— 目录**在**本机，那 2 个 skip
  掩盖的是一个路径缺陷而不是「本机没这个依赖」。不修的理由见 [docs/backlog.md](backlog.md) B1。


## 6. 最容易踩的坑（血泪版，完整清单在 CLAUDE.md）

1. **必须用 `.venv\Scripts\python.exe`** —— 系统 Python 没装 sherpa-onnx / soundfile。
2. **`contracts/voice-events.schema.json` 字节不变** —— 有 SHA-256 摘要测试钉死；平台事件走另一个契约文件。
3. **`VoiceState` 六态不改** —— 派发的并发发生在 `thinking` 内部，靠 `task.*` 事件报进度。
4. **音频回调里不许跑重活** —— `on_recognized` 只入队，`pump()` 才跑整轮；在回调里跑派发+TTS 会丢帧，表现为「识别器听错」而不是卡死。
5. **被拒绝的唤醒绝不开识别器** —— 否则等于转写未授权人声。有测试钉死。
6. **`confirmed` 必须 `is True`** —— `"no"` 是真值字符串。这条在 `shell.run` 上被抓过一次，**MCP 分支上同样成立**。
7. **控制台的三份白名单不许放宽** —— `EDITABLE` / `AGENT_EDITABLE` / `MCP_EDITABLE`。往里加 `command`、`url`、`require_confirmation`、`allow`、`auto_allow` 都是把一次安全决策降级成一次点击。加键前读 ADR 006 §3。
8. **确认面只有唤醒球一个** —— 控制台不给确认按钮，连命令原文都不显示。
9. **`enrollment/` 与 `memory/` 永不提交** —— 生物特征与个人数据，已在 `.gitignore`。
10. **控制台中文乱码不是缺陷** —— Windows 代码页显示问题，UTF-8 字节是对的，别去「修」。**但断言失败是缺陷** —— 两个 Python `str` 比较失败跟终端怎么显示无关。
11. **基线只在干净 shell 里成立** —— 记基线前先确认 `env | grep PYTHON` 为空。
12. **改了 `core/console/static/index.html` 只跑 pytest 不算验证** —— 那是单文件 HTML，测试碰不到它。渲染取证例程见 `docs/routines.md`。


## 7. 不受版本控制的目录（各自的处置不同）

**`github_program/` 是参考源，不要删**（2026-08-16 更正：本文件此前写成「无关目录可直接删」，那是错的）。
里面五个 checkout 各有用途，许可证不同，移植前必须先看许可证：

| 目录 | 许可证 | 用途 |
|---|---|---|
| `cc-switch-main` | MIT | agent/供应商嗅探与切换的参考实现（Tauri 2，同栈） |
| `hermes-agent-main` | MIT | 模型提供商配置、实时换模型、语音互转的参考 |
| `kkclaw-master` | **Claw Desktop Pet License（个人使用，禁止转售）** | UI 移植源。个人使用/修改明确许可；**禁止售卖或付费分发**，免费再分发须附许可证与版权声明，且不得用作者名/项目名/logo 做推广 |
| `open-design-main` | 未核实 | 未使用 |
| `opendex-main` | 未核实 | 未使用 |

`open-design-main.zip`、`.tmp-open-design*/` 是解压残留与临时日志，可删。
`models/` 约 597 MB（gitignore 内），其中 `kws.tar.bz2` / `tts.tar.bz2` / `asr.tar.bz2` 三个归档是解压完可删的。

## 8. 提交历史（本轮平台化的 11 个提交）

```text
0cacde4 feat(voice): 唤醒→聆听→转写 闭环接线
3a86d15 feat(asr): 流式语音识别提供器
1c3a736 feat(voice): 唤醒词打断(barge-in)接线
8ff6f8a feat(tts): 本地 TTS 播放接入语音路径
63a0a32 feat(desktop): 系统托盘
94ce0b7 feat(tts): 本地 TTS 合成提供器
5bf0b75 feat(desktop): P8 唤醒球事件通道与外壳
3c6a46b feat(memory): 记忆召回注入派发 + 短期层自裁剪
45ef21c feat(scripts): run_desktop.py
d3b7dba feat(platform): P6 派发层 + P7 ACP/HTTP 适配器 + VoiceRuntime 语音接线
206489b feat(agents): P5 agent 适配器
```

每个提交都是「代码 + 测试 + 文档同步」的完整单元，`git show <sha>` 可以单独读懂。
