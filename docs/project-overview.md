# 项目总览与当前进度

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-08-02(Phase 4 平台化 P0 骨架落地)
> 本文件是项目的单一入口(single source of truth,唯一事实来源),其它文档由此索引。

## 1. 项目是什么

**EvoX Voice Wake** 是面向 Windows 的**开放式语音唤醒对话平台**。目标交互链路:

```
说出唤醒词 → 声纹确认是本人 → 唤醒球弹出 → 本地识别语音
    ├─ 简单的事:平台自己做(读文件 / 搜索 / 终端 / 记忆),毫秒级
    └─ 复杂的事:派发给 claude / codex / opencode / hermes,路由汇总
→ 流式回复 → TTS 朗读 → 继续多轮对话 → 可随时打断
```

Phase 3 原型的定位是「EvoX 语音唤醒对话客户端」。Phase 4 起 EvoX **降级为众多 agent 后端之一**,平台不再绑定单一后端。未授权的声音在声纹门被**静默**挡下 —— 不弹球、不出声、不给任何反馈。

三条设计红线(贯穿全部实现):

1. **本地优先(local-first)** — 唤醒、端点检测(VAD)、语音合成全部在本机运行,项目代码不保存、不上传音频。声纹校验也落在本机,零云调用;记忆只存文本、永不存音频。
2. **组件可替换(replaceable providers)** — 唤醒、ASR、LLM、TTS、会话传输层、**agent 后端**都在契约(contract)之后,任何第三方 SDK 类型不得泄漏进公开事件结构。
3. **验证等级诚实(honest verification levels)** — 文档声明必须标注证据来源,自动化测试不得冒充真机验收。**mock 子进程不得冒充真实 agent**。

## 2. 当前状态速览

| 维度 | 状态 |
|---|---|
| 阶段 | Phase 3(原型与决策)已完成;**Phase 4(生产实现)进行中** —— P0 骨架已落,P1 声纹起 |
| 技术选型 | **已定案**,见 [ADR 001](adr/001-voice-stack-selection.md) ~ [ADR 005](adr/005-task-dispatch-model.md) |
| Python 测试 | **43 passed, 2 skipped**(skipped 为可选 VoxCord 依赖) |
| 前端构建 | `npm run build`(tsc + vite)通过;`cargo check` 通过 |
| 真机麦克风唤醒 | **已验证一次**(2026-07-26,`你好问问`,7.193s,score 1.0) |
| 声纹准入 | provider 与 fail-closed 路径 **AUTO 已验**;门未接线,模型未下载,真机未验 |
| 平台层四包 | **仅契约**(315 行 Protocol),无实现 |
| 真实 EvoX 会话桥接 | **未验证** — 发布阻塞项 |
| 真实外部 agent | **未验证** — 发布阻塞项(REAL-AGENT) |
| 真实透明窗口验收 | **未验证** — 发布阻塞项 |
| 版本控制 | **已 git init**,基线 `9f7d923`(Phase 3 原型固化)+ `9b5f029`(`core/audio` 拆包) |

## 3. 已定案的技术选型

| 层 | 选定方案 | 备选/降级 |
|---|---|---|
| 语音运行时 | **sherpa-onnx 1.13.4**(单一依赖边界,覆盖 KWS/VAD/ASR/TTS) | openWakeWord(唤醒)、faster-whisper / SenseVoiceSmall(ASR)、Kokoro-82M(TTS) |
| 唤醒(KWS) | sherpa-onnx-kws-zipformer-wenetspeech-3.3M | — |
| 端点检测(VAD) | Silero VAD(经 sherpa-onnx 执行) | — |
| 语音合成(TTS) | vits-melo-tts-zh_en(MeloTTS) | Kokoro-82M |
| EvoX 桥接 | `LocalEvoXTransport`(带认证的 localhost HTTP) | 任何实现 `ConversationTransport` 协议的传输层 |
| 声纹准入 | **3D-Speaker ERes2Net**(经 sherpa-onnx 执行,零新依赖) | 同系列 200k 通用模型 |
| agent 接入 | **headless CLI 子进程 + ACP** 双通路 | OpenAI 兼容 HTTP(含 OpenClaw Gateway) |
| 记忆存储 | **SQLite + FTS5**(单文件)+ Markdown 人类可读层 | — (明确不做向量检索) |
| 派发模式 | **`single`(默认)/ `race`** | `fanout` 仅显式请求多方验证时 |
| UI 渲染 | **Canvas 2D(v1 主路径)** + CSS 玻璃层 | 静态 CSS(降级档)、WebGL shader(v2 升级路径) |
| 桌面外壳 | Tauri 2 + TypeScript + Vite | — |

## 4. 已完成的工作

### Phase 1–2:调研与选型
- `docs/research/evox-community.md` — EvoX/EvoMap 社区入口排查,**未发现可验证的原生语音资产**。
- `docs/research/open-source-landscape.md` — 候选方案实地核查。
- `docs/research/selection-matrix.md` — 9 维度加权打分矩阵(语音核心 + UI 技术两张表)。

### Phase 3:原型与决策(t10–t13,本轮完成)
- **t10 语音栈整合验证** — `tmp_proto/t10_voice_stack_validation.py`,6 项全部通过。
- **t11 渲染路线原型** — `tmp_proto/orb_spike.html`,三条路线全部可用。
- **t12 结果记录** — `prototype-results.md` 补充验证等级图例、环境记录、复现命令索引。
- **t13 ADR 定案** — 最终语音组件、桥接方式、渲染路线、备选实现、发布阻塞项。

### Phase 4:平台化 P0 骨架(2026-08-02)
- **`core/audio` 拆包** — 295 行的 `providers.py` 按职责拆成 6 个模块,`providers.py` 留 29 行重导出薄壳,现有导入零改动。
- **声纹 provider 补完** — `embed` / `enroll` / `verify` / `remove` / `describe` / `close`,fail-closed 四条路径全部有测试。
- **`core/events.py` 抽出** — 信封的唯一构造与校验点,事件枚举运行时从契约文件读,不在 Python 里镜像。
- **平台层四包建骨架** — `agents` / `dispatch` / `tools` / `memory`,只有契约无实现。
- **测试边界归位** — t10 的行为断言升格 `tests/integration/`,需真实硬件的脚本移入 `scripts/acceptance/`。
- **ADR 002–005 定案** — 声纹准入、agent 接入协议、记忆架构、任务派发模型。

### 已实现的代码骨架

| 模块 | 文件 | 行数 | 说明 |
|---|---|---:|---|
| 事件契约 | `contracts/voice-events.schema.json` | 14 | 9 种事件类型,版本 `"1"`(**字节不变**) |
| 状态机 | `core/state.py` | 53 | 6 状态 + 严格转移表(**不扩展**) |
| 事件构造 | `core/events.py` | 93 | 信封唯一构造点 + 契约校验 |
| 语音提供器 | `core/audio/`(6 模块 + `__init__`) | 688 | KWS/VAD/采集/**声纹**/VoxCord |
| 重导出薄壳 | `core/providers.py` | 29 | 保旧导入路径不断 |
| 会话桥接 | `core/session_bridge.py` | 92 | `ConversationTransport` 协议 + HTTP 实现 |
| 平台层契约 | `core/{agents,dispatch,tools,memory}/contract.py` | 315 | **仅 Protocol,无实现** |
| 插件门面 | `evox_plugin/plugin.py` | 191 | EvoX 工具面 + 回合编排 |
| 前端 | `desktop/src/main.ts` + `style.css` | 35 | 唤醒球与状态标签 |
| 窗口 | `desktop/src-tauri/src/main.rs` | 31 | 透明、置顶、不占任务栏 |
| 测试 | `tests/*.py` + `tests/integration/` | 661 | 45 用例,见 [测试文档](testing.md) |

## 5. 进行中 / 下一步

**Phase 4 分十阶段,顺序原则:能纯 AUTO 验证的先做,依赖外部 CLI 的次之,依赖真实硬件的最后。**

| 阶段 | 内容 | 证据等级 | 状态 |
|---|---|---|---|
| P0 | 骨架:声纹 provider、`events.py`、四包契约、测试归位、ADR 与文档 | AUTO | ✅ 完成 |
| P1 | **声纹门**:环形缓冲、fail-closed 门、录入 CLI、`wake.rejected` 产出点、真实 score | AUTO | 🔄 下一步 |
| P2 | 平台事件契约:`agent-events.schema.json` + `agents.schema.json` | AUTO | ⬜ |
| P3 | 记忆系统 `core/memory/` | AUTO | ⬜ |
| P4 | 本地工具 `core/tools/` + `config/tools.toml` | AUTO | ⬜ |
| P5 | agent 适配器 `cli.py` + `evox.py` | AUTO+SIM | ⬜ |
| P6 | 派发/路由/汇总 `core/dispatch/` | AUTO+SIM | ⬜ |
| P7 | `acp.py` + `http.py` / `openclaw.py` | AUTO | ⬜ |
| P8 | 唤醒球弹出 + Canvas 2D 渲染器 + 工具确认 UI | REAL-WIN | ⬜ |
| P9 | 真实 agent 联调(`claude` / `opencode` 各一次) | REAL-AGENT | ⬜ |
| P10 | 真实语音端到端(含他人拒绝) | REAL-MIC + REAL-AGENT | ⬜ |

原 Phase 4 计划里的「流式 ASR、TTS 播放队列、超时重连、状态机生产化」并入 P1–P8 各阶段,不单列。

## 6. 发布阻塞项(release blockers)

以下每一项都必须有**真机证据**才能关闭。第 1–7 项见 [ADR 001](adr/001-voice-stack-selection.md#required-before-release-blockers),第 8–10 项为 Phase 4 平台化新增:

| # | 阻塞项 | 当前等级 | 需要达到 |
|---|---|---|---|
| 1 | 中文唤醒**质量**验收(安静/远场/噪声/重复) | 单次 REAL-MIC | 多场景 REAL-MIC 统计 |
| 2 | 真实麦克风 Silero 端点检测验收 | 设备开合已验证 | REAL-MIC 语音端点 |
| 3 | **真实 EvoX 会话桥接**(发送/增量回复/取消/超时/重连) | SIM(mock 传输) | REAL-EVOX |
| 4 | 真实流式首字延迟 | 未测 | REAL-EVOX 实测 |
| 5 | **独立透明置顶窗口**(合成/DPI 125-175%/多显示器/托盘/远程桌面) | 定义并编译通过 | REAL-WIN |
| 6 | 持续运行资源画像(≥30 分钟 CPU/内存/FPS) | 未测 | REAL-WIN 长跑 |
| 7 | 提供器可替换性(契约强制,无 SDK 类型泄漏) | AUTO 已验证 | 保持 |
| 8 | **声纹准入实测**(本人通过 / 他人拒绝球不弹 / 录音回放) | AUTO(fail-closed 与 store) | REAL-MIC([ADR 002](adr/002-speaker-verification.md)) |
| 9 | **真实外部 agent 跑通一轮** | 仅契约 | REAL-AGENT([ADR 003](adr/003-agent-integration-protocol.md)) |
| 10 | **工具安全实机**(`shell.run` 确认含拒绝路径、误唤醒防护) | 仅契约 | AUTO 全绿 + REAL-WIN([ADR 005](adr/005-task-dispatch-model.md)) |

## 7. 文档地图

| 文档 | 用途 |
|---|---|
| **本文件** | 项目总览、进度、阻塞项(入口) |
| [architecture.md](architecture.md) | 技术架构、组件边界、事件契约、数据流 |
| [requirements.md](requirements.md) | 功能/非功能需求、验收标准、阶段范围 |
| [testing.md](testing.md) | 测试环境、命令、分层、已验证结果与待验收矩阵 |
| [routines.md](routines.md) | 可重复编码例程(改完什么跑什么) |
| [adr/001-voice-stack-selection.md](adr/001-voice-stack-selection.md) | 选型决策记录与发布阻塞项 |
| [adr/002-speaker-verification.md](adr/002-speaker-verification.md) | 声纹准入:为什么零新依赖、门在 KWS 命中时、fail-closed、静默拒绝 |
| [adr/003-agent-integration-protocol.md](adr/003-agent-integration-protocol.md) | agent 接入:ACP + headless CLI 双通路,OpenClaw 作后端而非底座 |
| [adr/004-memory-architecture.md](adr/004-memory-architecture.md) | 记忆:SQLite + FTS5,为什么不做向量与知识图谱 |
| [adr/005-task-dispatch-model.md](adr/005-task-dispatch-model.md) | 派发:路由五维、汇总策略、`fanout` 不做默认、工具政策门 |
| [research/prototype-results.md](research/prototype-results.md) | 原型实测数据与验证等级 |
| [research/selection-matrix.md](research/selection-matrix.md) | 候选方案加权打分 |
| [research/open-source-landscape.md](research/open-source-landscape.md) | 开源候选实地核查 |
| [research/evox-community.md](research/evox-community.md) | EvoX 原生资产排查 |
| [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | 第三方组件与模型许可证 |

## 8. 已知风险与注意事项

- **`shell.run` 是全项目最大的安全风险面** — 「语音说一句话就能在本机执行命令」。声纹门砍掉「他人语音」这一支,但**误识别与录音回放仍然存在**,所以默认关闭 + 白名单 + 每次 UI 确认 + 危险模式拦截 + 审计日志五层一条都不能省(ADR 005)。
- **声纹不防录音回放** — 本轮不做反欺骗模型,这是**已知缺口**而非尚未测到(ADR 002 局限节)。
- **声纹注册数据是生物特征** — `enrollment/` 已在 `.gitignore` 内,永不提交;查看注册状态只用 `describe()`。
- **VoxCord 不在本机** — `D:\program\voxcord` 不存在,相关测试自动 skip;它是可选参考依赖,不影响发布路径。
- **模型体积大** — `models/` 约 413 MB(含两个未清理的 `.tar.bz2` 归档共 192 MB),再加声纹模型 37 MB;打包策略需在 P8 前决定。多 agent 子进程并发另有内存压力,P6 需加派发并发上限。
- **开源项目判定多为「社区来源」** — `github.com` / `api.github.com` / `raw.githubusercontent.com` 的 WebFetch 在本环境全部被拦截,无法读取一手 README。除注明「官方文档确认」者外,star 数、许可证、最后提交时间均未直接核实,不得当官方结论用。
- **SenseVoiceSmall 权重许可证未取证** — 若启用该 ASR 备选,须先归档 ModelScope 许可证文本。
- **控制台中文乱码** — Windows 代码页显示问题,UTF-8 字节本身正确,不是数据缺陷。
