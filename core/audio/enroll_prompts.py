"""注册声纹时念的句子 —— 脚本和控制台**共用这一份**。

## 为什么是六条不同的长句

段数：`SpeakerEmbeddingManager.add()` 实测是**求质心**（两条正交向量注册在一个名字下各自
只得 0.7071 = 1/√2），所以多段样本是在平均掉每次说话的偶然偏差。实测（真实人声 + CAM++，
见 `docs/research/prototype-results.md`）：1 段 0.706 / 2 段 0.772 / 3 段 **0.794**，单调上升。

内容：**六句各不相同，不是同一句念六遍。** 同一句多遍练出来的质心对那一句的音素组合过拟合，
而唤醒时说的是唤醒词、之后说的是任意请求。句子也刻意写长（13–17 字，约 3 秒）—— 短句
的 embedding 不稳，实测 0.8 s 的窗只有 0.59–0.67，1.5 s 才到 0.68–0.80。

距离：最后两条要求**退开两步**。只用近场注册时，远一点说话的相似度掉到 0.607；把一段远场
一并注册进来能抬到 0.722，而近场那一侧没有变差（SIM，加噪近似远场）。

两条带唤醒词、四条不带：门实际听的是「以唤醒词结尾的 1.5 秒」，所以让唤醒词进档案是对的；
但全带就又变成对一句话过拟合了。
"""

from __future__ import annotations

#: 平时唤醒的距离。
NEAR = "平时唤醒的距离"
#: 退开两步，音量照常 —— 不要刻意喊，喊出来的声音和平时不是一个人。
FAR = "往后退两步，音量照常"

#: （条件, 句子）。顺序有意义：前四条近场，后两条远场。
ENROLL_ROUNDS: tuple[tuple[str, str], ...] = (
    (NEAR, "你好小沃，帮我看一下今天的日程安排"),
    (NEAR, "现在几点了，外面天气怎么样"),
    (NEAR, "帮我打开网易云音乐，放一首轻松的歌"),
    (NEAR, "检查一下目前的运行状态是否正常"),
    (FAR, "小沃小沃，把刚才那句话再说一遍"),
    (FAR, "今天下午三点提醒我去开会，谢谢"),
)

#: 默认录几段。等于全部六条 —— 少录也能注册，但样本越少质心越飘。
DEFAULT_ROUNDS = len(ENROLL_ROUNDS)


def rounds(count: int | None = None) -> tuple[tuple[str, str], ...]:
    """前 ``count`` 条。要得比六条多时循环取，但**条件顺序保持**（近场在前）。"""
    total = DEFAULT_ROUNDS if count is None else max(1, int(count))
    if total <= DEFAULT_ROUNDS:
        return ENROLL_ROUNDS[:total]
    return tuple(ENROLL_ROUNDS[index % DEFAULT_ROUNDS] for index in range(total))


def as_json(count: int | None = None) -> list[dict[str, str]]:
    """给控制台的形状。纯数据。"""
    return [{"condition": condition, "text": text} for condition, text in rounds(count)]


__all__ = ["DEFAULT_ROUNDS", "ENROLL_ROUNDS", "FAR", "NEAR", "as_json", "rounds"]
