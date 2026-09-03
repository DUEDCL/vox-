"""让 agent 会用工具 —— 但**只能用注册过的那些**。

## 为什么要有这一层

使用者的实测：他说「试了给我打开网易云音乐」，正则没命中（ASR 把「帮我」听成了「试了」），
落到 agent，agent 回了一句 **「好，正在打开网易云音乐。」然后什么都没发生**。

那不是模型在撒谎 —— 系统提示里明明写着「你**没有**文件系统、终端和网络……真需要动这台
机器时直接说『这个我做不到』」。一个被告知自己无能为力的模型，面对一句明确的请求，最容易
的出路就是说得像做过了。这是**结构问题不是措辞问题**：请求能到它那里，能力却不在它手上。

使用者选的路是「两者结合」：正则走快路径（0 秒、可预测），没命中再让 agent 带着工具去做。
这个模块是后半截。

## 三条设计，每条都有它反面的坏处

1. **清单是白名单，不是「所有工具」。** `REGISTERED` 列的是 agent 能点名的工具；不在里面的
   （比如 `shell.run`）它连名字都看不到。理由不是「模型会乱来」，而是**能力面要可枚举**：
   一个能被模型点名的工具集合如果等于「运行时恰好装了什么」，那它会随配置漂移，而没有人
   在改配置时想着「这会不会让 agent 多一个能力」。

2. **调用是一行纯文本，不是 provider 的 function calling。** OpenAI 的 `tools=[]`、
   Anthropic 的 `tool_use`、ACP 的 `tool_call` 三种形状各不相同，而 `AgentDescriptor` /
   `Task` / `AgentChunk` 的字段只许标量与 Mapping（红线 2）。一行文本对**四种后端一视同仁**
   —— CLI 的 `claude`、HTTP 的中转站、ACP、EvoX 都只需要会写字。代价是模型偶尔写错格式，
   而那时它退化成一句普通回答（不是一次崩溃）。

3. **执行仍然走 `ToolRunner` 和它那套闸门。** 这一层只**解析**，不放行。`shell.run` 照样要
   已验证说话人 + 确认卡，`fs.read` 照样在沙箱里。所以「agent 会用工具」没有扩大任何一个
   安全边界 —— 它扩大的是**谁能发起**，而不是**什么能被执行**。

## 调用格式

    ⟦vox:tool app.open {"name": "网易云"}⟧

方括号那两个字符（U+27E6/U+27E7）是刻意挑的：它们不在中文标点里、不在代码里常见、
也不会从语音转写里冒出来，所以**不可能被误触发**。JSON 参数便于精确传值。

一轮最多一次调用（`MAX_CALLS`）。不是技术限制而是延迟预算：每一轮 agent 是 2–20 秒，
两次调用就是一分钟，而使用者在等一句话。

证据等级：AUTO（纯函数 + 假 runner）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

#: agent 能点名的工具。**白名单** —— 不在这里的它连名字都看不到。
#:
#: `shell.run` 刻意不在：它要的是一次人工确认，而确认卡是给**使用者说的那句话**准备的界面。
#: 一个由模型发起、由使用者确认的命令执行，混淆了「谁想跑这条命令」——
#: 而那正是确认卡要回答的唯一问题。
REGISTERED: tuple[str, ...] = ("app.open", "web.open", "web.search", "fs.read", "time.now")

#: 一轮最多几次工具调用。延迟预算，不是技术限制：每一轮 agent 是 2–20 秒。
MAX_CALLS = 1

#: 调用标记。U+27E6/U+27E7 不在中文标点里、不在代码里常见、也不会从语音转写里冒出来 ——
#: 所以它**不可能被误触发**。
OPEN = "⟦"
CLOSE = "⟧"

_CALL = re.compile(
    OPEN + r"\s*vox:tool\s+(?P<tool>[a-z][a-z0-9_.]*)\s*(?P<args>\{.*?\})?\s*" + CLOSE,
    re.IGNORECASE | re.DOTALL,
)

#: 每个工具怎么用，一句话。给模型看的，所以写的是**它要填什么**而不是实现。
_HOW: Mapping[str, str] = {
    "app.open": '打开本机应用或网站。{"name": "网易云"}；放歌再带 {"query": "薛之谦"}',
    "web.open": '在浏览器里打开一个地址或搜索。{"query": "bilibili.com"}',
    "web.search": '搜网页并拿回摘要。{"query": "sherpa-onnx 版本"}',
    "fs.read": '读一个沙箱内的文件。{"path": "README.md"}',
    "time.now": "查当前时间。不需要参数：{}",
}


def manifest(available: Sequence[str] = ()) -> str:
    """给系统提示用的工具清单。``available`` 是运行时**真的装了**的那些。

    两个集合求交而不是直接印 `REGISTERED`：印一个这台机器上不存在的工具，模型会去调它，
    然后拿回一句「工具没装」—— 那一轮就白花了 2–20 秒。空交集时返回空串，
    调用方据此完全不提工具这件事（那时的正确行为就是老的「我做不到」）。
    """
    usable = [name for name in REGISTERED if not available or name in set(available)]
    if not usable:
        return ""
    lines = [f"- {name}：{_HOW.get(name, '')}" for name in usable]
    return (
        "你可以让 Vox 替你做这几件事。**要做就只输出一行调用，不要同时写解释**：\n\n"
        f"    {OPEN}vox:tool 工具名 {{\"参数\": \"值\"}}{CLOSE}\n\n"
        + "\n".join(lines)
        + "\n\n三条规矩：一轮**最多一次**调用；调用那一行之外不要写别的字；"
        "结果会回给你，那时再用一句话告诉用户结果。做不到的事仍然直接说做不到，不要假装做了。"
    )


def parse_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """从回答里挑出工具调用。**纯函数。**

    参数解析失败时**跳过这一条**而不是抛：模型写坏一个花括号不该让整轮失败，而那时它退化
    成一句普通回答 —— 那是这个格式最好的失败模式。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for match in _CALL.finditer(str(text or "")):
        name = str(match.group("tool") or "").strip().lower()
        if name not in REGISTERED:
            continue
        raw = match.group("args")
        arguments: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, Mapping):
                continue
            # 值只收标量：一个嵌套结构进到工具参数里，等于让模型决定工具内部的形状。
            arguments = {
                str(key): value
                for key, value in parsed.items()
                if isinstance(value, (str, int, float, bool))
            }
        found.append((name, arguments))
        if len(found) >= MAX_CALLS:
            break
    return found


def strip_calls(text: str) -> str:
    """把调用那一行去掉，剩下的才是能念给人听的。

    模型有时会在调用旁边写一句「好，我来开」。留着它的后果是使用者听到两遍
    （一遍是这句，一遍是工具执行完之后的汇报）。
    """
    return _CALL.sub("", str(text or "")).strip()


def render_result(tool: str, ok: bool, detail: str) -> str:
    """把工具结果写成回给模型的一段话。

    带上 `ok` 是因为**失败必须让模型知道**：拿不到这一位它会把「拒绝了」当成「做完了」，
    然后向用户汇报成功 —— 那正是这整个模块要修的那个毛病。
    """
    head = "成功" if ok else "失败"
    body = str(detail or "").strip() or "（没有输出）"
    return f"{OPEN}vox:result {tool} {head}{CLOSE}\n{body[:1200]}"


__all__ = [
    "CLOSE",
    "MAX_CALLS",
    "OPEN",
    "REGISTERED",
    "manifest",
    "parse_calls",
    "render_result",
    "strip_calls",
]
