"""服务商预设表 —— 模型配置那一栏的下拉来源。

## 为什么需要这张表

控制台原来只有「端点／模型名」两个空输入框。填对一家服务商要同时知道三件事：
兼容模式的路径（各家都不一样：`/v1`、`/api/paas/v4`、`/api/v3`、`/compatible-mode/v1`）、
该用哪种协议形状、以及密钥从哪个环境变量读。三件里错一件，表现都是同一句
「连不上」，查起来很慢。

## 端点**未经本项目实测**

这张表的端点抄自各家公开文档与 `duchenlin-blog/src/data/providers.ts`（那边标注
2026-08-25 用 curl 逐家打过一次）。**本项目没有复验过**，所以不要把它当已验证事实：
控制台上那颗「探一下」按钮才是判据，它真的发一次 `GET {base}/models` 并把
状态码与耗时显示出来。`401`/`403` 是好结果（主机在、路径对、只是密钥不对），
`404` 说明路径拼错了，超时说明这条路当前不通。

## 这里永远不写密钥

与 `config/agents.toml` 同一条规矩：预设里只有**环境变量名**（`key_env`），
值一律从环境变量读。写进配置文件的 key 会进版本库、进日志、进事件流。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Proto = Literal["openai", "anthropic", "ollama", "dashscope", "custom"]


@dataclass(frozen=True)
class Provider:
    """一家服务商的预设。`slug` 是配置文件里引用它的名字。"""

    slug: str
    name: str
    #: 兼容模式端点，已含版本段，后面直接接 `/chat/completions`
    base: str
    #: 请求形状。国内这几家的兼容模式都是 OpenAI 那套
    proto: Proto
    #: 从哪个环境变量读密钥
    key_env: str
    #: 服务器在中国大陆。**不改变请求怎么发**，只影响界面怎么解读超时
    domestic: bool = False
    #: 本机服务，不需要密钥
    local: bool = False
    note: str = ""


#: 大语言模型。`custom` 那一条留给自定义端点，界面上把三个输入框放开。
LLM: tuple[Provider, ...] = (
    Provider("openai", "OpenAI", "https://api.openai.com/v1", "openai", "VOX_LLM_KEY"),
    Provider("anthropic", "Anthropic", "https://api.anthropic.com/v1", "anthropic", "VOX_LLM_KEY"),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("moonshot", "月之暗面 Kimi", "https://api.moonshot.cn/v1", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("zhipu", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("dashscope", "阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("volcengine", "火山方舟", "https://ark.cn-beijing.volces.com/api/v3", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("minimax", "MiniMax", "https://api.minimax.chat/v1", "openai", "VOX_LLM_KEY", domestic=True),
    Provider("ollama", "Ollama（本机）", "http://127.0.0.1:11434/v1", "ollama", "", local=True,
             note="本机服务，不需要密钥；明文 HTTP 只许回环地址"),
    Provider("custom", "自定义", "", "custom", "VOX_LLM_KEY",
             note="端点、协议、密钥环境变量全部自己填"),
)

#: 语音识别。本项目默认走**本机** sherpa-onnx，云端只是备选。
ASR: tuple[Provider, ...] = (
    Provider("sherpa-local", "sherpa-onnx（本机，默认）", "", "custom", "", local=True,
             note="流式 zipformer-zh-14M，模型在 models/ 下；本地优先是这个项目的第一条红线"),
    Provider("openai-whisper", "OpenAI Whisper", "https://api.openai.com/v1", "openai", "VOX_ASR_KEY"),
    Provider("dashscope", "阿里云百炼 Paraformer", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai", "VOX_ASR_KEY", domestic=True),
    Provider("custom", "自定义", "", "custom", "VOX_ASR_KEY"),
)

#: 语音合成。同样默认本机。
TTS: tuple[Provider, ...] = (
    Provider("sherpa-local", "sherpa-onnx（本机，默认）", "", "custom", "", local=True,
             note="VITS 中文单说话人，模型在 models/ 下"),
    Provider("openai", "OpenAI TTS", "https://api.openai.com/v1", "openai", "VOX_TTS_KEY"),
    Provider("dashscope", "阿里云百炼 CosyVoice", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai", "VOX_TTS_KEY", domestic=True),
    Provider("custom", "自定义", "", "custom", "VOX_TTS_KEY"),
)

KINDS: dict[str, tuple[Provider, ...]] = {"llm": LLM, "asr": ASR, "tts": TTS}


def find(kind: str, slug: str) -> Provider | None:
    """按 kind + slug 取一条预设。找不到返回 None（调用方决定是不是错误）。"""
    for p in KINDS.get(kind, ()):
        if p.slug == slug:
            return p
    return None


def as_json(kind: str) -> list[dict[str, object]]:
    """给前端的形状。**不含任何密钥值**，只有环境变量名。"""
    return [
        {
            "slug": p.slug,
            "name": p.name,
            "base": p.base,
            "proto": p.proto,
            "key_env": p.key_env,
            "domestic": p.domestic,
            "local": p.local,
            "note": p.note,
        }
        for p in KINDS.get(kind, ())
    ]
