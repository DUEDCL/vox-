# ADR 007：MCP 工具接入

> 状态：已采纳（2026-08-28）
> 相关：[ADR 003 agent 接入协议](003-agent-integration-protocol.md) · [ADR 005 派发与工具门](005-task-dispatch-model.md) · [ADR 006 本地控制台](006-local-console.md)

## 背景

平台自带三个工具（`fs.read` / `web.search` / `shell.run`）。生态里已经有大量 MCP
（Model Context Protocol）server 提供现成能力，而接入它们的传输形状 ——
**JSON-RPC 2.0 over stdio** —— 和 `core/agents/acp.py` 已经在讲的那一种完全相同。

所以这是一次扩展，不是新子系统。

## 决策

新增 `core/tools/mcp.py`：一个 stdio JSON-RPC 客户端，把远端工具包装成本地 `Tool`，
挂进现有的 `ToolRunner`。配置在 `config/mcp.toml`，形状由
`contracts/mcp.schema.json` 校验。

### 1. 同一道门，没有第二条路

红线 2 说「agent 拿不到用户语音拿不到的能力」。这句话反过来读也成立：**一个远端工具
不该拿到本地工具拿不到的许可**。所以 `mcp.<server>.<tool>` 经过的是检查 `fs.read` 的
那同一个 `DefaultToolPolicy.check()`，产出的是同一批 `tool.*` 事件，审计落同一个长期层。

`TOOL_NAMES` 是固定枚举而 MCP 工具名是运行时发现的，所以 `check()` 里加了一个
`mcp.` 前缀分支 —— 治它的开关在配置里，不在 frozenset 里。

### 2. 三层默认关

| 层 | 出厂值 | 作用 |
|---|---|---|
| `[mcp] enabled` | `false` | 总开关。关着时 `mcp.*` 一个都不注册，policy 也一律拒 |
| `[[servers]] enabled` | `false` | 每个 server 自己的开关 |
| `require_confirmation` | `true` | 每次调用都要在唤醒球上确认 |

**默认要确认，这是和 `shell.run` 同一个起点。** 一个 MCP 工具能做的事没有上界 ——
写文件、发网络请求、改数据库 —— 所以起始假设不能是「只读」。收窄的唯一方式是把工具名
写进该 server 的 `auto_allow`，那是一个具名的决定。

`confirmed` 的判定是 `is True` 而不是真值 —— `"confirmed": "no"` 是个真值字符串，这个
缺陷在 `shell.run` 上被测试抓过一次，不能在新表面上重现。

### 3. 名字空间被约束，不是自由文本

工具名是 `mcp.<server>.<tool>`，三段。两端都校验字符集：

- server 名必须是小写 slug（`^[a-z0-9][a-z0-9_-]{0,31}$`）—— 否则一个叫 `fs.read`
  的 server 就能伪造内置工具的段名；
- 远端工具名必须匹配 `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`，不合的**在 `tools/list`
  时就被丢掉**（含点的名字同样是伪造段名的路径）。

policy 按第一个点分段，所以每个 MCP 工具都落在 `mcp` 段，一个开关治全部。

### 4. 拒绝时说「unknown tool」，不说「那个 server 关着」

server 缺失、被关、名字拼错，三种情况都回同一句。区分它们等于让调用方能枚举这台机器
配了哪些 server。

### 5. 凭据不继承，远端输出不可信

- 子进程拿 `scrubbed_env()`（和 `shell.run`、和 agent 子进程同一个按标记丢弃的实现）。
  要给某个 server 传 key 只能在 `env_passthrough` 里写**变量名**。配置文件**没有放
  密钥的键**，schema 层面就不存在。
- 强制 `PYTHONUTF8` / `PYTHONIOENCODING`：MCP 帧**由协议规定**是 UTF-8，而 Windows 上
  Python 子进程默认按 ANSI 代码页编 stdout。这与 `acp.py` 的立场一致，残留缺口也一致
  （非 Python 的 server 写本地代码页照样烂，环境里没有变量能命令任意程序改编码）。
- 远端返回的文本是**不可信输入**：按 `max_output_bytes` 截断、不进事件 payload、
  非文本内容（图片、资源链接）被**命名**而不是丢掉（返回空字符串会读作「工具什么都没做」）。
- `allow` 名单在**运行时复检**，不只在注册时过滤。这是内置工具已有的纪律
  （`fs.read` 即使 policy 查过路径也会自己再 resolve 一次），它让一个被交给自建 runner
  的工具仍然有界。

### 6. 一个 server 起不来是警告，不是失败

MCP 是本地工具的**补充**，不是它们的前提。一个不可达的 server 不该拖住其他 server 或
整个平台。每个被跳过的 server 按名字上报，好让「我打开了它但什么都没发生」有答案。

握手失败时子进程被收尸 —— 一个拒绝了握手的 server 不能留在那里跑。

### 7. 生命周期归 `ToolRunner`

MCP server 是本进程的子进程，除了 `ToolRunner` 没有别的对象有资格结束它们。所以
`runner.mcp` 持有 registry，`runner.close()` 收尸，`VoiceRuntime._close_resource` 会
调到它。

启动发生在 `open_tools()` 而不是懒启动：一个会在第一次调用后变长的工具列表会让
「这个平台能做什么」变成一个依赖时间的问题。

## 后果

- 平台的能力面可以由用户扩展，而不需要改 Vox 的代码。
- 证据等级 **SIM**：测试驱动一个讲这四种形状的进程内假 server（51 例，
  `tests/test_mcp.py`）。**没有任何第三方 MCP server 通过这个客户端完成过一次调用** ——
  那是一条新的 REAL 级验收项，记进发布阻塞项。
- `config/mcp.toml` 出厂两个示例条目**全部注释掉**，所以默认安装既不起子进程也不联网。

## 被否决的方案

- **把 MCP 当成第五种 agent 适配器**：MCP 是工具协议（`tools/list` / `tools/call`），
  不是对话协议。硬套 `AgentChunk` 会需要把工具调用伪装成回复流。
- **给 MCP 工具一条自己的策略**：那正是红线 2 反过来读要禁的事。
- **默认免确认，让用户自己去开确认**：默认值是大多数人唯一会用的值。一个默认放行的
  远端工具通路比没有这个通路更糟。
- **在配置里放 token 键**：`agents.toml` 已经为这件事做过决定，理由不变。
