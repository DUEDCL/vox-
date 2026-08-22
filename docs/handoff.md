# 交接文档 —— Vox

> 交接时间：2026-08-23  ·  最近代码提交：`f223008`（DesktopBridge 生命周期加固）
> 验证基线：Python **625 passed, 3 skipped**（628 collected）· DesktopBridge 专项 **33 passed** · Rust **15 passed** · `npm run build` 通过
> 基线在**干净 shell**（未设置 `PYTHONUTF8` / `PYTHONIOENCODING`）下复现；数字取决于环境变量的话就不是基线。
> 本文件是**接手者的第一份材料**。项目全貌看 [`docs/project-overview.md`](project-overview.md)，
> 干活的规矩看 [`.claude/CLAUDE.md`](../.claude/CLAUDE.md)（那份是硬约束，本文件只做导航）。

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

语音五段在**代码级全部打通，且各自经真实模型验证过**；但**没有一次是在你的麦克风+扬声器前跑完整轮的**。

| 环节 | 实现 | 已验证到 | 缺什么 |
|---|---|---|---|
| 唤醒（KWS） | `core/audio/kws.py` | 真机单次唤醒（2026-07-26）+ 合成音触发 | 多场景统计 |
| 声纹门 | `core/audio/speaker.py` | AUTO：判别力 0.736/0.370、阈值 0.5 落间隙 | 本人通过率 / 他人拒绝 / 回放 |
| 流式 ASR | `core/audio/asr.py` | 真实模型转写 bundled wav 成功 | 真机麦克风转写 |
| 派发 / 路由 / 汇总 | `core/dispatch/` | AUTO+SIM 159 用例 | 真实 agent 跑一轮（见下） |
| TTS 合成 + 播放 | `core/audio/tts.py` `playback.py` | 真实模型合成 44.1kHz / 236ms | 扬声器出声 |
| 打断（barge-in） | `plugin.wake_detected` + `cancel` | AUTO（fake TTS） | 真机说话打断 |
| 唤醒球 / 托盘 / 事件通道 | `desktop/` + `core/desktop_bridge.py` | DesktopBridge 专项 33 + cargo test 15 + npm build | 透明窗口 / DPI / 点击 |

**REAL-AGENT 为什么还欠着（2026-08-16 实测）**：`claude` CLI 在 PATH（2.1.223），但**从子进程调起时返回 `Not logged in · Please run /login`（exit 1）** —— 嵌套调用拿不到当前会话的凭据。所以这条不是「还没试」，是**试过且被登录状态挡住**。要拿到 REAL-AGENT 证据，得先让 `claude` 在一个独立的、已登录的非嵌套环境里可用（或改用 `codex` / `opencode` / `evox` 任一已登录的后端）。

**一句话**：代码是齐的，**REAL 级证据是缺的**。接手后最有价值的第一件事不是写新功能，是把 §4 的真机验收跑一遍。

## 3. 代码地图（92 个受版本控制的代码文件，按「改这里要看什么」组织）

```text
core/
  audio/        设备与模型：kws 唤醒 · asr 流式识别 · tts 合成 · playback 播放
                capture 采集(两段模式) · speaker 声纹 · ring 环形缓冲 · vad
  agents/       agent 适配器：cli · acp · http · evox + registry/schema/contract
  dispatch/     intent 意图 · router 五维路由 · aggregator 汇总 · breaker 熔断 · dispatcher
  memory/       store(SQLite+FTS5) · write · recall · contract
  tools/        policy 安全门 · fs · web · shell · runner
  events.py     ★ 事件信封唯一构造点
  state.py      ★ 六态状态机（不许改）
  desktop_bridge.py  Python↔唤醒球的管道通道
vox_plugin/
  plugin.py     VoicePlugin 门面（状态机 + 记忆 + 工具 + TTS + capture 的 attach_*）
  runtime.py    ★ VoiceRuntime：把插件/派发/工具/记忆/唤醒球/麦克风装到一起
contracts/      voice-events(9 种，字节不变) · agent-events(12 种) · agents.schema
desktop/        Tauri 2 唤醒球：src/main.ts 前端 · src-tauri/src/main.rs 窗口+托盘+事件通道
scripts/        run_desktop.py 命令行 · acceptance/ 真机验收脚本 · enroll_speaker.py 声纹录入
```

**读代码的建议顺序**：`core/state.py`（6 态）→ `core/events.py`（信封）→ `vox_plugin/plugin.py`（门面）
→ `vox_plugin/runtime.py`（装配，看 `say()` 和 `pump()`）→ 再按需要下钻到 `core/dispatch/` 或 `core/audio/`。

## 4. 接手后第一件事：真机验收（需要你在场）

按这个顺序跑，每一步都**看返回值而不是「没报错」**：

```powershell
# 0) 环境自检（应当 625 passed, 3 skipped；DesktopBridge 专项应当 33 passed）
#    先确认环境里没有 PYTHONUTF8 / PYTHONIOENCODING —— 基线只在干净 shell 里成立
.\.venv\Scripts\python.exe -m pytest tests -q --basetemp .pytest-run

# 1) 录入你的声纹（必须本人，读 3~5 句）
.\.venv\Scripts\python.exe scripts/enroll_speaker.py --name <你的名字>

# 2) 只测唤醒（说「你好问问」）
.\.venv\Scripts\python.exe scripts/acceptance/live_wake.py

# 3) 全链路：唤醒 → 说请求 → 派发 → 出声 → 再说唤醒词打断
.\.venv\Scripts\python.exe scripts/acceptance/live_conversation.py

# 4) 唤醒球（先构建，再看透明/置顶/点击/托盘）
Push-Location desktop; npm run build; Pop-Location
Push-Location desktop/src-tauri; cargo build; Pop-Location
.\.venv\Scripts\python.exe scripts/run_desktop.py
```

跑完把**实测数字**写进 [`docs/research/prototype-results.md`](research/prototype-results.md)，
并按真实等级更新 [`docs/project-overview.md`](project-overview.md) 第 6 节的 11 项发布阻塞项。
**不要**把跑通一次写成「已验证」——多场景统计才是。

## 5. 已知缺口（照实写，不要假设存在）

- **`web.search` 没有真实后端** —— 每个托管搜索 API 都是带 key 的云依赖，撞红线 1，故意不内置；未注入时工具报 `no search backend is configured`。
- **TTS 多段排队** —— 长回复目前一次性合成播放，没有按句切分排队。
- **Canvas 2D 生产渲染器** —— 现在是 DOM + CSS（六态与动画都已落地，够用）；ADR 001 里的 Canvas 主路径还没写。
- **超时 / 重连 / 错误恢复** —— 桥接与 agent 的失败已是 chunk，但没有重连策略。
- **声纹反欺骗** —— 门不防录音回放，这是 ADR 002 明确记下的局限，**不是待办**。
- **记忆跨进程持久性** —— Markdown 往返同进程内已验，重启后未验。
- **子进程输出编码只修了一半** —— Windows 上子进程默认按 ANSI 代码页（本机 cp936）编 stdout，父进程按 UTF-8 读且 `errors="replace"`，于是乱码变成夹在正常回复里的 U+FFFD：**静默错，不报错**。`acp.py` 已给 Python 子进程注入 `PYTHONUTF8`/`PYTHONIOENCODING`（ACP 协议规定 UTF-8，所以强制是对的）；**非 Python 的子进程无解** —— 环境里没有变量能命令一个任意程序改 stdout 编码。已知受影响面：`shell.run` 读原生控制台工具（`git` / `dir`）的中文输出（当前 `enabled=false` + 空白名单，够不到）。`claude` 实测不受影响（Node 恒写 UTF-8）。

## 6. 最容易踩的坑（血泪版，完整清单在 CLAUDE.md）

1. **必须用 `.venv\Scripts\python.exe`** —— 系统 Python 没装 sherpa-onnx / soundfile。
2. **`contracts/voice-events.schema.json` 字节不变** —— 有 SHA-256 摘要测试钉死；平台事件走另一个契约文件。
3. **`VoiceState` 六态不改** —— 派发的并发发生在 `thinking` 内部，靠 `task.*` 事件报进度。
4. **音频回调里不许跑重活** —— `on_recognized` 只入队，`pump()` 才跑整轮；在回调里跑派发+TTS 会丢帧，表现为「识别器听错」而不是卡死。
5. **被拒绝的唤醒绝不开识别器** —— 否则等于转写未授权人声。有测试钉死。
6. **`shell.run` 的 `confirmed` 必须 `is True`** —— `"no"` 是真值字符串，按真值判断会直接执行。
7. **`enrollment/` 与 `memory/` 永不提交** —— 生物特征与个人数据，已在 `.gitignore`。
8. **控制台中文乱码不是缺陷** —— Windows 代码页显示问题，UTF-8 字节是对的，别去「修」。**但断言失败是缺陷** —— 两个 Python `str` 比较失败跟终端怎么显示无关，别拿这条规则把真的编码 bug 放过去（2026-08-16 就抓到一个）。
9. **基线只在干净 shell 里成立** —— 记基线前先确认 `env | grep PYTHON` 为空。`599 passed, 2 skipped` 这个旧数字就是在设了 `PYTHONUTF8` 的 shell 里记的，干净 shell 下当时是 `1 failed`。

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
