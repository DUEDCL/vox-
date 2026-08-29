# Backlog —— 识别但故意没做的技术债

> 这份清单只记**已经查清根因、并且明确决定这一轮不动**的项。
> 「还没做的功能」不在这里，那些在 `docs/project-overview.md` 第 5 节。

## B1. `VoxCordAdapter` 的 sys.path 拼装与 voxcord 当前布局不匹配

**发现日期**：2026-08-28
**状态**：不修（可选依赖，不在发布路径上）

`docs/project-overview.md` 与 `.claude/CLAUDE.md` 长期写着「`D:\program\voxcord` 不存在」。
这是**错的事实陈述**，虽然结论（不可用）碰巧对。实测：

```
VoxCordAdapter().load()
→ available=False, reason="import failed: No module named 'voxcord_core'"
```

目录**在**本机（`D:\program\voxcord`，monorepo：`apps/desktop` React+Tauri /
`packages/voxcord_core`）。根因是 `core/audio/voxcord.py` 往 `sys.path` 加的是
`packages/voxcord_core` 与 `packages/voxcord_core/lib`，而真实模块在
`packages/voxcord_core/lib/audio_engine/`，顶层名 `voxcord_core` 只有在那个包被
`pip install -e .` 之后才存在。

所以 `tests/test_provider_adapter.py` 里那 2 个 skip **掩盖了一个路径缺陷**，而不是
在报告「本机没有这个可选依赖」。

**为什么不修**：VoxCord 是可选参考依赖，不在发布路径上（`project-overview.md` 第 8 节
已如此记载）。修它要么改 sys.path 拼装（然后需要一个真的能 import 的布局来验证），要么
要求使用者 `pip install -e` 那个包 —— 两条都超出「完善本项目」的边界。

**修的时候需要什么**：一台装了 voxcord 依赖的机器，以及把那 2 个 skip 变成真断言。

## B2. 回环 URL 校验有四份副本，该抽成 `core/urls.py`

**发现日期**：2026-08-28
**状态**：不重构（会动三个已测的安全边界模块）

同一条规则（绝对 HTTP(S) / 无内嵌凭据 / 明文 HTTP 仅回环）现在有四份实现：

| 位置 | 异常类型 | 备注 |
|---|---|---|
| `core/session_bridge.py:71` `_validate` | `BridgeError` | 还捆了 token 必需检查 |
| `core/agents/http.py:144` `_validate_url` | `HttpAgentError` | 消息里带 agent 名 |
| `core/tools/search_backends.py` `endpoint_problem` | 返回原因字符串 | 英文消息 |
| `core/models_config.py` `url_problem` | 返回原因字符串 | **第四份**，消息是中文（直接显示在控制台上） |

四份并存的原因是**各自的错误语境不同**，而且前两份的错误消息有测试钉死。抽公共函数
要编辑三个安全边界模块，只为给第四个加功能 —— 按「修 bug 只修 bug，不顺手重构周边」，
本轮不做。

**每多一份，这一项的价值就上升一档。** 第四份出现时的判断是「仍然不值得动三个已测模块」，
第五份出现时应该先抽再加。

**该怎么抽**：一个 `problem(url) -> str | None` 的纯函数放 `core/urls.py`，四个调用方
各自包装成自己的异常并保留自己的消息文本（中英文各自留在调用方）。四处的现有测试一条
都不该改。

## B3. 控制台的视觉语言待与唤醒球会话统一

**发现日期**：2026-08-28
**状态**：留着（并行会话占用设计文档）

`core/console/static/index.html` 的色值是这个文件自己的，**没有**从
`desktop/src/style.css` 抄（那是另一个产品表面，它的色值由素材取样定，控制台没有那条
依据）。语义上只借了一件事：琥珀表示「等你处理」。

按 `D:\program\CLAUDE.md` 的判等级，新页面属 L 级，该写一条
`docs/design/DESIGN_DECISIONS.md`。本轮**没写** —— 那份文档正被做唤醒球 UI 的并行会话
使用，两个会话同时写同一个文件会冲突。

**要做的**：等 UI 会话收尾后，补一条 DD 记录控制台的三条视觉立场（不是仪表盘 / 行式
清单 / 不抄球的色值），并在 `DESIGN_SYSTEM_STATUS.md` 里给控制台一次八轴评分。

## B4. `tts.chunk` 事件带正文，这是设计而非泄漏 —— 但值得复查一次

**发现日期**：2026-08-28
**状态**：不改（既有契约行为）

控制台的事件流面板（第一版有、第二版已删）曾把 `tts.chunk` 的 payload 显示出来，
一件既有事实因此变得显眼：**`tts.chunk` 的 payload 就是要朗读的整句文本**。所以一轮
读文件的回合会把文件内容逐句放进事件流。面板删掉之后这件事不再显眼了 —— 但事实没变，
所以这一条留着。

这不是本轮引入的，也不违反「事件带决定不带内容」—— `tts.chunk` 的定义就是「要说的
文本」，没有它下游就不知道该合成什么。控制台侧已按 240 字截断（可读性，不是隐私）。

**值得复查的是**：`tts.chunk` 会扇出到每一个 sink，包括未来可能新增的日志与传输通道。
如果哪天加了一个落盘日志 sink，这条就变成「对话正文落盘」。届时的正确做法是给 sink
分级，而不是改 `tts.chunk`。

## B5. `scripts/fetch_models.py` 未实现

**发现日期**：2026-08-28
**状态**：形状已定，代码未写（见 `docs/model-distribution.md` 第 2.5 节）

三条硬约束已经写下来了：每个文件必须有 SHA-256 并在下载后校验、URL 必须是上游官方
地址不设镜像默认、默认不自动跑。当前只有声纹模型的摘要在 `THIRD_PARTY_NOTICES.md`
里，其余三个要在实现时补齐。

## B6. 控制台第二版没有开麦克风的入口

**发现日期**：2026-08-28
**状态**：按使用者的取舍留着（不是遗漏）

第一版就绪清单里有一行 `microphone` 加一颗「启动麦克风」按钮。第二版按使用者点名的
六个模块重做，麦克风面板在被砍的名单里（同批砍掉的还有事件流面板、记忆检索面板和
五个单项测试按钮；砍掉的面板连同 `index.html` 里对应的 DOM 一起删，不是隐藏）。
后端的 `mic_start` / `mic_stop` **保留**，因为 `scripts/run_console.py --voice`
仍然调 `mic_start`。

所以现在开麦克风的唯一方式是启动时带 `--voice`。**声纹注册不受影响** —— 它走浏览器的
`getUserMedia`，和采集线程是两条路。

**要恢复的话**：就绪清单那张卡上加一颗按钮打 `POST /api/mic/start`，端点已经在。
先问使用者要不要，别自己加回来。

## B7. `models.toml` 的 `active` 不能从控制台切换

**发现日期**：2026-08-28
**状态**：与界面保持一致（界面明写「保存后不会自动切换生效」）

`PUT /api/models` 只写 `profiles.*` 下的五个字段，不写顶层 `active`。所以一套新方案能
从页面上建好、填好、探通，但**切换生效仍然要手动改一行**。页面上如实写了这件事
（`mp-note`：「你正在看的是另一套，保存后不会自动切换生效」）。

不做的理由是范围：切 `active` 等于切整个语音栈用哪套模型，而目前**没有任何运行时代码
读 `models.toml`** —— 语音栈仍然由 `config/voice.toml` + 四个环境变量决定。先让读侧存在，
再让切换有意义；否则那颗按钮改的是一个没人读的字段。

**做的时候需要什么**：一个真的按 `models.toml` 组装 ASR/TTS/LLM 的读侧（现在只有
`load_models_config` 一个 loader），然后 `active` 才值得有一颗按钮。
