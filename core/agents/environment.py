"""发给裸 LLM 端点的 system prompt：它自己猜不到的那些事。

## 为什么需要这个文件

`kind = "http"` 的后端是一个**裸的 Chat Completions 端点** —— 没有工具、没有文件系统、
不知道自己被谁调用。此前 `http.py` 一条 system message 都不发，于是模型看到的唯一
system prompt 是**端点自己注入的那份**。实测（2026-08-29，走生产同一条路问 relay 的原话）：

> 我的 system prompt 里包含一段系统环境信息，说明操作系统是 linux，当前工作目录是 `/`。

那是中转站容器的情况，不是这台机器的。后果是它给 Windows 用户建议
`netease-cloud-music &`、操心 X11/Wayland 和 PulseAudio —— 一段语法正确、事实全错的回答。

`kind = "cli"` 的后端**不需要**这个：`claude` / `codex` / `opencode` 是本机进程，
自己就知道操作系统和工作目录。所以这里只服务 HTTP 那一条路。

## 两条约束，缺一条都会出错的那种

1. **回答会被 TTS 念出来。** Markdown 标题、项目符号、代码块念出来是噪音；一段 800 字的
   回答念完要两分多钟。所以要求口语短句。这条同时修掉「语音回复听起来不全」的一半原因 ——
   一个短回答是能被念完的回答。
2. **它动不了这台机器。** 打开应用、读文件、跑命令由 Vox 自己的工具做。不说清这一点，
   模型会把「怎么做」当成「已经做了」来写，而用户听到的是一段无法执行的指令。
"""

from __future__ import annotations

import platform


def describe_host() -> str:
    """这台机器的一句话事实。只报 ``platform`` 真答得出来的东西。

    不报工作目录：对一个没有文件系统的端点说「当前目录是 X」正是要修掉的那类谎。
    """
    system = platform.system() or "unknown"
    release = platform.release() or ""
    return f"{system} {release}".strip()


#: 裸 LLM 端点的 system message。
#:
#: 刻意不做成配置项：它是正确性修复而不是偏好。写在这里而不是散在 ``http.py`` 里，
#: 是因为它需要解释自己为什么存在，而那段解释比它本身长。
SPEECH_SYSTEM_PROMPT = """\
你是 Vox 的对话内核。Vox 是一个运行在 {host} 上的本地语音助手：用户说话，本机识别成
文字交给你，你的回答会被本机的语音合成**朗读出来**。

关于你的处境，有三件事必须记住：

1. 你运行的机器是 {host}。不是 Linux，没有容器，没有 X11 或 PulseAudio。提到路径时用
   Windows 形式。
2. 你**没有**文件系统、终端和网络。打开应用、读文件、查时间、开网页这些事由 Vox 自己的
   本地工具完成，不由你完成。所以不要写「运行以下命令」然后给一段 shell —— 用户是在用
   耳朵听，他没法执行它，而且那件事本来该由 Vox 直接做。真需要动这台机器时，直接说
   「这个我做不到」或者说清要用哪个功能，别给操作步骤。
3. 你的回答要**能被念出来**：口语、短句、通常两三句话说完。不用 Markdown 标题、不列项目
   符号、不出代码块、不用表情符号。数字和单位写成读得出来的样子（「三点二十」而不是
   「15:20」）。用户用什么语言问就用什么语言答。

需要写代码或者做长任务时，说明这件事该交给子代理，而不是自己把代码念出来。"""


def speech_system_prompt() -> str:
    """当前这台机器的 system message。"""
    return SPEECH_SYSTEM_PROMPT.format(host=describe_host())


__all__ = ["SPEECH_SYSTEM_PROMPT", "describe_host", "speech_system_prompt"]
