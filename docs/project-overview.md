# 项目总览与当前进度

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-07-28(Phase 3 原型与决策阶段收尾)
> 本文件是项目的单一入口(single source of truth,唯一事实来源),其它文档由此索引。

## 1. 项目是什么

**EvoX Voice Wake** 是面向 Windows 的 EvoX 语音唤醒对话原型。目标交互链路:

```
说出唤醒词 → 本地识别语音 → 送入 EvoX 会话 → 得到回复 → TTS 朗读 → 继续多轮对话 → 可随时打断
```

三条设计红线(贯穿全部实现):

1. **本地优先(local-first)** — 唤醒、端点检测(VAD)、语音合成全部在本机运行,项目代码不保存、不上传音频。
2. **组件可替换(replaceable providers)** — 唤醒、ASR、LLM、TTS、会话传输层都在契约(contract)之后,任何第三方 SDK 类型不得泄漏进公开事件结构。
3. **验证等级诚实(honest verification levels)** — 文档声明必须标注证据来源,自动化测试不得冒充真机验收。

## 2. 当前状态速览

| 维度 | 状态 |
|---|---|
| 阶段 | Phase 3(原型与决策)**已完成**;Phase 4(生产实现)**未开始** |
| 技术选型 | **已定案**,见 [ADR 001](adr/001-voice-stack-selection.md) |
| Python 测试 | **19 passed, 2 skipped**(skipped 为可选 VoxCord 依赖) |
| 前端构建 | `npm run build`(tsc + vite)通过;`cargo check` 通过 |
| 真机麦克风唤醒 | **已验证一次**(2026-07-26,`你好问问`,7.193s,score 1.0) |
| 真实 EvoX 会话桥接 | **未验证** — 发布阻塞项 |
| 真实透明窗口验收 | **未验证** — 发布阻塞项 |
| 版本控制 | **当前目录不是 git 仓库**(`git status` 报 not a git repository) |

## 3. 已定案的技术选型

| 层 | 选定方案 | 备选/降级 |
|---|---|---|
| 语音运行时 | **sherpa-onnx 1.13.4**(单一依赖边界,覆盖 KWS/VAD/ASR/TTS) | openWakeWord(唤醒)、faster-whisper / SenseVoiceSmall(ASR)、Kokoro-82M(TTS) |
| 唤醒(KWS) | sherpa-onnx-kws-zipformer-wenetspeech-3.3M | — |
| 端点检测(VAD) | Silero VAD(经 sherpa-onnx 执行) | — |
| 语音合成(TTS) | vits-melo-tts-zh_en(MeloTTS) | Kokoro-82M |
| EvoX 桥接 | `LocalEvoXTransport`(带认证的 localhost HTTP) | 任何实现 `ConversationTransport` 协议的传输层 |
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

### 已实现的代码骨架

| 模块 | 文件 | 行数 | 说明 |
|---|---|---:|---|
| 事件契约 | `contracts/voice-events.schema.json` | 14 | 9 种事件类型,版本 `"1"` |
| 状态机 | `core/state.py` | 53 | 6 状态 + 严格转移表 |
| 语音提供器 | `core/providers.py` | 295 | KWS/VAD/麦克风采集/VoxCord 适配器 |
| 会话桥接 | `core/session_bridge.py` | 92 | `ConversationTransport` 协议 + HTTP 实现 |
| 插件门面 | `evox_plugin/plugin.py` | 198 | EvoX 工具面 + 回合编排 |
| 前端 | `desktop/src/main.ts` + `style.css` | 35 | 唤醒球与状态标签 |
| 窗口 | `desktop/src-tauri/src/main.rs` | 31 | 透明、置顶、不占任务栏 |
| 测试 | `tests/*.py`(6 个文件) | 337 | 见 [测试文档](testing.md) |

## 5. 进行中 / 下一步

**Phase 4(生产实现,t14 起)尚未开始**,计划顺序:

1. 契约固化与版本兼容测试
2. 核心状态机生产化(超时、错误恢复、重连)
3. 提供器补全(流式 ASR、TTS 播放队列、打断)
4. 渲染器生产实现(t28–t35:Canvas 2D 渲染器 + 质量分档选择器)
5. 窗口行为完善(t53:真机透明度、DPI、多显示器、托盘)
6. 真实 EvoX 桥接联调

## 6. 发布阻塞项(release blockers)

以下每一项都必须有**真机证据**才能关闭,详见 [ADR 001](adr/001-voice-stack-selection.md#required-before-release-blockers):

| # | 阻塞项 | 当前等级 | 需要达到 |
|---|---|---|---|
| 1 | 中文唤醒**质量**验收(安静/远场/噪声/重复) | 单次 REAL-MIC | 多场景 REAL-MIC 统计 |
| 2 | 真实麦克风 Silero 端点检测验收 | 设备开合已验证 | REAL-MIC 语音端点 |
| 3 | **真实 EvoX 会话桥接**(发送/增量回复/取消/超时/重连) | SIM(mock 传输) | REAL-EVOX |
| 4 | 真实流式首字延迟 | 未测 | REAL-EVOX 实测 |
| 5 | **独立透明置顶窗口**(合成/DPI 125-175%/多显示器/托盘/远程桌面) | 定义并编译通过 | REAL-WIN |
| 6 | 持续运行资源画像(≥30 分钟 CPU/内存/FPS) | 未测 | REAL-WIN 长跑 |
| 7 | 提供器可替换性(契约强制,无 SDK 类型泄漏) | AUTO 已验证 | 保持 |

## 7. 文档地图

| 文档 | 用途 |
|---|---|
| **本文件** | 项目总览、进度、阻塞项(入口) |
| [architecture.md](architecture.md) | 技术架构、组件边界、事件契约、数据流 |
| [requirements.md](requirements.md) | 功能/非功能需求、验收标准、阶段范围 |
| [testing.md](testing.md) | 测试环境、命令、分层、已验证结果与待验收矩阵 |
| [routines.md](routines.md) | 可重复编码例程(改完什么跑什么) |
| [adr/001-voice-stack-selection.md](adr/001-voice-stack-selection.md) | 选型决策记录与发布阻塞项 |
| [research/prototype-results.md](research/prototype-results.md) | 原型实测数据与验证等级 |
| [research/selection-matrix.md](research/selection-matrix.md) | 候选方案加权打分 |
| [research/open-source-landscape.md](research/open-source-landscape.md) | 开源候选实地核查 |
| [research/evox-community.md](research/evox-community.md) | EvoX 原生资产排查 |
| [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | 第三方组件与模型许可证 |

## 8. 已知风险与注意事项

- **当前目录不是 git 仓库** — 无版本历史与回滚能力,建议在进入 Phase 4 前初始化。
- **VoxCord 不在本机** — `D:\program\voxcord` 不存在,相关测试自动 skip;它是可选参考依赖,不影响发布路径。
- **模型体积大** — `models/` 约 413 MB(含两个未清理的 `.tar.bz2` 归档共 192 MB),打包策略需在 Phase 4 决定。
- **SenseVoiceSmall 权重许可证未取证** — 若启用该 ASR 备选,须先归档 ModelScope 许可证文本。
- **控制台中文乱码** — Windows 代码页显示问题,UTF-8 字节本身正确,不是数据缺陷。
