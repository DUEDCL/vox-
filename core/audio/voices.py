"""云端 TTS 的音色表。

## 为什么是一张钉住的表，而不是一次 API 调用

使用者要求「tts 模型配置应该能拉取到目标模型的音色」。核实的结果是：**百炼没有列举
系统预置音色的 API。** 系统音色只在文档页上（`help.aliyun.com/zh/model-studio/
cosyvoice-voice-list`）；有列表 API 的是**声音复刻**（`mini-clone-api` 的「查询音色
列表」），那个只列你自己复刻出来的音色，不含系统预置的。

所以这里给的是一张**带出处和核实日期**的表，控制台从它填下拉建议。这不是「拉取」，
差别要说清楚：表可能过期，而端点不会。为此音色字段在界面上是**可输入的建议列表**
（`<datalist>`）而不是封闭下拉 —— 表里没有的音色照样能填进去，由「试一句」去验证，
而不是由这张表决定什么合法。

## 核实记录

- 来源：`help.aliyun.com/zh/model-studio/cosyvoice-voice-list`，2026-08-29 抓取。
- `cosyvoice-v1` 全部 20 个系统音色，整套**不支持方言、不支持 SSML/Instruct/时间戳**，
  只在**华北2（北京）**可用。
- 使用者点名的 `longyuan` = **龙媛**，中文，v1 里存在，推荐场景是「有声书、语音助手、
  聊天数字人」。文档在 `longyuan_v2` / `longyuan_v3` 上才标「温暖治愈女」这个特质，
  所以那句描述属于新版本而不是 v1。

## 一条没解决的矛盾，留在这里而不是藏起来

非实时 HTTP 接口文档列出的合法 `model` 是 `qwen-audio-3.0-tts-plus/flash`、
`cosyvoice-v3.5-plus/flash`、`cosyvoice-v3-plus/flash`、`cosyvoice-v2` ——
**没有 `cosyvoice-v1`**。而使用者的免费额度是 `cosyvoice-v1`（10K 字符、永不过期），
`longyuan` 又是 v1 的音色名。

这意味着「v1 + longyuan」有可能只能走 WebSocket 实时接口，或者要在 v2 上换成
`longyuan_v2`。**这一条只能由真实请求来判**，不能靠读文档定，所以
`scripts/probe_dashscope_tts.py` 会把几种组合都打一遍，报哪个回 200。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 出处，写进代码是为了下一个人不用再找一遍。
VOICE_LIST_SOURCE = "https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list"
VOICE_LIST_CHECKED = "2026-08-29"


@dataclass(frozen=True)
class Voice:
    """一个系统预置音色。``languages`` 是文档写的语言，不是猜的。"""

    voice: str
    name: str
    languages: str
    scenes: str = ""


#: ``cosyvoice-v1`` 的 20 个系统音色（上面那一页的全量抄录，顺序保持文档顺序）。
COSYVOICE_V1: tuple[Voice, ...] = (
    Voice("longwan", "龙婉", "中文", "语音助手、导航播报、聊天数字人"),
    Voice("longcheng", "龙橙", "中文", "语音助手、导航播报、聊天数字人"),
    Voice("longhua", "龙华", "中文", "语音助手、导航播报、聊天数字人"),
    Voice("longxiaochun", "龙小淳", "中文、英文", "语音助手、聊天数字人"),
    Voice("longxiaoxia", "龙小夏", "中文", "语音助手、聊天数字人"),
    Voice("longxiaocheng", "龙小诚", "中文、英文", "语音助手、聊天数字人"),
    Voice("longxiaobai", "龙小白", "中文", "聊天数字人、有声书"),
    Voice("longlaotie", "龙老铁", "中文东北口音", "新闻播报、聊天数字人"),
    Voice("longshu", "龙书", "中文", "有声书、新闻播报"),
    Voice("longshuo", "龙硕", "中文", "语音助手、新闻播报"),
    Voice("longjing", "龙婧", "中文", "语音助手、新闻播报"),
    Voice("longmiao", "龙妙", "中文", "客服、语音助手"),
    Voice("longyue", "龙悦", "中文", "语音助手、有声书"),
    Voice("longyuan", "龙媛", "中文", "有声书、语音助手、聊天数字人"),
    Voice("longfei", "龙飞", "中文", "会议播报、新闻播报"),
    Voice("longjielidou", "龙杰力豆", "中文、英文", "新闻播报、聊天数字人"),
    Voice("longtong", "龙彤", "中文", "有声书、聊天数字人"),
    Voice("longxiang", "龙祥", "中文", "新闻播报、有声书"),
    Voice("loongstella", "Stella", "中文、英文", "语音助手、客服"),
    Voice("loongbella", "Bella", "中文", "语音助手、客服"),
)

#: ``qwen-audio-3.0-tts-plus`` / ``-flash`` 的系统音色。
#:
#: **这一组是实测出来的，不是抄文档的** —— 那两个音色列表页在本环境 WebFetch 打不开
#: （`help.aliyun.com` / `www.alibabacloud.com` / `platform.qianwenai.com` 全部被拦）。
#: 所以做法是拿真实端点逐个试：20 个候选音色名里**只有 `longanhuan_v3.6` 回 200**，
#: 其余全是 411 `[cosyvoice:]Engine error [411]: TTS speak operation failed`。
#:
#: 文档里那条约束解释了为什么试错率这么高：**每个 model 只支持一组特定的 voice，
#: 不能把一个模型的音色用在另一个模型上。** 所以 cosyvoice 那 20 个名字（含 longyuan）
#: 在这个模型上一个都不成立。
#:
#: `longanhuan_v3.6` 是女声：合成一句话测基频中位数 **253 Hz**（女声区间 165–255 Hz）。
QWEN_AUDIO_TTS: tuple[Voice, ...] = (
    Voice(
        "longanhuan_v3.6",
        "龙安焕",
        "中文",
        "语音助手、聊天数字人（实测基频中位数 253 Hz，女声；qwen-audio-3.0-tts-plus 上唯一验通的音色）",
    ),
)

#: 支持 ``instruction``（自由指令控制语气）的模型。文档明写只有这两个。
#: 音色决定「是谁在说」，instruction 决定「她怎么说」—— 要温柔就用它，不要去换音色。
INSTRUCTION_MODELS: frozenset[str] = frozenset(
    {"qwen-audio-3.0-tts-plus", "qwen-audio-3.0-tts-flash"}
)

#: 非实时 HTTP 接口文档列出的合法 model 值 + 使用者免费额度里那个 v1。
#:
#: 顺序按**本机实测可用性**排：第一个是 2026-08-29 唯一打通的组合。
#: v1 留在最后并标注：文档的合法值里没有它，而使用者的额度是它 —— 实测它回
#: 400 `current user api does not support http call`，即 v1 只走 WebSocket。
CLOUD_TTS_MODELS: tuple[str, ...] = (
    "qwen-audio-3.0-tts-plus",
    "qwen-audio-3.0-tts-flash",
    "cosyvoice-v2",
    "cosyvoice-v3-flash",
    "cosyvoice-v3-plus",
    "cosyvoice-v3.5-flash",
    "cosyvoice-v3.5-plus",
    "cosyvoice-v1",
)


def voices_for(model: str) -> tuple[Voice, ...]:
    """这个 model 支持哪一组音色。**混用会 411**，所以按模型分组是必需的。"""
    name = str(model or "").strip().lower()
    if name.startswith("qwen-audio-3.0-tts"):
        return QWEN_AUDIO_TTS
    return COSYVOICE_V1


def voice_options(model: str = "") -> list[dict[str, str]]:
    """给控制台的音色建议表。纯数据，不打网络。

    传了 ``model`` 就只给那个模型的一组 —— 给一个「全部音色」的下拉是在邀请 411。
    """
    table = voices_for(model) if model else COSYVOICE_V1 + QWEN_AUDIO_TTS
    return [
        {
            "voice": item.voice,
            "name": item.name,
            "languages": item.languages,
            "scenes": item.scenes,
        }
        for item in table
    ]


def describe_voice(voice: str) -> Voice | None:
    """按 ``voice`` 参数找一条。找不到返回 ``None`` —— 表里没有不等于不能用。"""
    for item in COSYVOICE_V1 + QWEN_AUDIO_TTS:
        if item.voice == voice:
            return item
    return None


__all__ = [
    "CLOUD_TTS_MODELS",
    "COSYVOICE_V1",
    "INSTRUCTION_MODELS",
    "QWEN_AUDIO_TTS",
    "VOICE_LIST_CHECKED",
    "VOICE_LIST_SOURCE",
    "Voice",
    "describe_voice",
    "voice_options",
    "voices_for",
]
