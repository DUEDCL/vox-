"""不用说「记住」它也记得住 —— 而且**不是什么都记**。

使用者的要求：「我把我的个人网站告诉他，下次对话他就能直接记住，而不是我说了『给我记住我的
个人网站』他才会记住。」

这一层做两件事：从一句话里**抽出候选**，再用**证据闸门**决定它能不能进长期层。两件事分开
是全部的重点 —— 只抽不筛的助手会把一次口误、一个临时决定、甚至别人说的一句话固化成它对你
的认识。Hermes 引的那份测量说，例行记忆保存把短期污染固化为长期记忆的比例最高到 **91%**，
所以闸门不是洁癖。

## 三条晋升通道（借 Hermes 的形状，原则是「宁紧勿松」）

| 通道 | 什么时候算 | 例子 |
|---|---|---|
| ``explicit`` | 使用者**直接陈述**一件关于自己的稳定事实，或明说「以后都这样」 | 「我的个人网站是 duchenlin.com」「以后都用中文回我」 |
| ``repeated`` | 同一条候选在**两个不同会话**里出现过 | 前天说过一次「我在北京」，今天又说 |
| ``correction`` | 使用者纠正了刚才那次回答 | 「不是这个，是 X」 |

三条满足其一就进长期层。只出现过一次、又不是直接陈述的（「今天想吃辣的」）留在候选里等
下一次证据 —— 它可能是真的，但**它现在还不该改变助手的行为**。

## 为什么是正则不是模型

抽取跑在**每一轮结束之后**，而一个每轮都要多跑一次 LLM 的记忆层会把回合时间翻倍。Hermes
把整理放到心跳里正是为了这个（「整理刻意不放在对话进行中，避免用户每轮多等几秒」）。规则
的代价是覆盖面窄 —— 但一条抽错的记忆比一条没抽到的贵得多，而规则的错法是可预测的。

模式只认**第一人称的自述**：「我的 X 是 Y」「我叫 Y」「我在 Y」「我用 Y」「我喜欢 Y」。
第二人称和第三人称一律不收 —— 「你的网站是什么」不是一条关于使用者的事实，而这个区别
是这一层最容易错的地方。

## 存在哪

候选进短期层（``kind="candidate"``），晋升后写进 ``write_fact()``（中层，会镜像成
Markdown）。**没有第三张表** —— 稳定层就是已经存在的 facts，控制台上能看能改能删的那一份。

证据等级：AUTO（纯函数 + 假 store）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 晋升通道。名字进 tag，所以它是可查的 —— 「这条记忆凭什么在这里」要能回答。
EXPLICIT = "explicit"
REPEATED = "repeated"
CORRECTION = "correction"

#: 候选在短期层里的 kind。和 ``turn`` 分开，否则它会被 ``recent_turns`` 当成对话读回去。
CANDIDATE_KIND = "candidate"

#: 一条候选最短多少字才算像话。太短的（「我的名」）是转写截断的产物。
MIN_STATEMENT = 4

#: 一条候选最长多少字。长句里往往裹着一整段请求，而记忆要的是可复述的一句。
MAX_STATEMENT = 120

#: 「以后都这样」这一类 —— 使用者在给一条**长期指令**，不是在说这一轮想要什么。
_STANDING = re.compile(
    r"(以后(都|就|请)?|从(现在|今天)(起|开始)|记住|别忘了|默认(就)?|每次都|下次也)"
)

#: 纠正 —— 「不是…是…」「说错了」。它把刚才那条候选的置信度顶上去。
#:
#: 「不是这个」单独成句也算：实测「不是这个，我的网站是 X」里，「是」不紧跟在后面，
#: 而那句话显然是一次纠正。
_CORRECTION = re.compile(
    r"(不是(这个|那个)|不是[,，]?\s*是|说错了|搞错了|应该是|我是说|不对[,，])"
)

#: 第一人称自述。**只认「我」开头的那一类**：第二人称问句（「你的网站是什么」）不是一条
#: 关于使用者的事实，而那正是这一层最容易错的地方。
#:
#: 每条模式必须捕获一个 ``value`` 组 —— 抽不到值的模式会把整句话当成事实存进去。
#:
#: 值一律写成「非标点、非贪婪、到标点或句尾停」。**不能用 ``\S``**：「我平时用的是
#: VS Code」会被切成「VS」，而一条抽了一半的事实比没抽到更糟 —— 它看起来是对的。
_VALUE = r"(?P<value>[^，,。；;！!？?]{1,24}?)(?=[，,。；;！!？?]|$)"

_SELF_FACTS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "site",
        re.compile(r"我(?:的)?(?:个人)?(?:网站|主页|博客|站点)(?:是|叫|在|：|:)\s*" + _VALUE),
        "个人网站",
    ),
    (
        "name",
        re.compile(r"我(?:的名字)?(?:叫做|叫|就叫)\s*" + _VALUE),
        "名字",
    ),
    (
        "city",
        # 「住在」「来自」是明确的；**裸的「在」不行** —— 「我在写一个语音助手」会被抽成
        # 「所在地：写一个语音助手」（实测）。裸「在」只在**句尾一个短词**时收，
        # 那时它几乎只能是地名。
        re.compile(
            r"我(?:现在)?(?:住在|来自)\s*" + _VALUE
            + r"|我(?:现在)?在\s*(?P<value2>[^，,。；;！!？?]{2,6})$"
        ),
        "所在地",
    ),
    (
        "tool",
        re.compile(r"我(?:平时|一般|习惯)?(?:用的是|用|使用)\s*" + _VALUE),
        "常用的",
    ),
    (
        "like",
        re.compile(r"我(?:比较|挺|很)?(?:喜欢|偏好|爱用)\s*" + _VALUE),
        "偏好",
    ),
    (
        "dislike",
        re.compile(r"我(?:不太|不)(?:喜欢|想要|想|要)\s*" + _VALUE),
        "不喜欢",
    ),
    (
        "birthday",
        re.compile(r"我(?:的)?生日(?:是|在|：|:)\s*" + _VALUE),
        "生日",
    ),
    (
        "job",
        # **不收裸的「我是」** —— 「我是杜沉麟」会被抽成「职业：杜沉麟」。
        re.compile(r"我(?:在做|从事|的职业是|的工作是)\s*" + _VALUE),
        "职业",
    ),
)

#: 这些开头一律不抽。它们是**问句或假设**，不是陈述 —— 「我的网站是什么」被当成事实存下来
#: 之后，助手会开始相信使用者的网站叫「什么」。
_NOT_A_STATEMENT = re.compile(r"(什么|哪个|哪里|多少|吗[?？]?$|呢[?？]?$|如果|要是|假如)")


@dataclass(frozen=True)
class Candidate:
    """一条候选记忆。

    ``statement`` 必须**脱离这次对话也能读懂** —— 那是 Hermes 给候选记忆定的硬约束，
    理由很实在：召回时它会被直接拼进 prompt，而「就用那个」在三天后没有任何意义。
    """

    key: str
    statement: str
    kind: str
    channel: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def promotable(self) -> bool:
        return bool(self.channel)


def _too_odd(value: str) -> bool:
    """一个抽出来的值像不像转写噪声。"""
    stripped = value.strip(" 。，,.;；:：!！?？")
    if len(stripped) < 1:
        return True
    # 纯标点、纯语气词。「我喜欢的是」这类抽出来的尾巴会落在这里。
    return stripped in {"的", "了", "吧", "呢", "啊", "嗯", "这个", "那个", "什么"}


def extract(text: str) -> list[Candidate]:
    """一句话 -> 候选记忆（可能是空的）。**纯函数。**

    同一句话里可以抽出多条（「我叫杜，我的网站是 x.com」），但同一个 ``key`` 只留第一条 ——
    后面那个多半是同一件事的复述。
    """
    body = str(text or "").strip()
    if not body:
        return []
    standing = bool(_STANDING.search(body))
    correcting = bool(_CORRECTION.search(body))
    found: dict[str, Candidate] = {}

    for key, pattern, label in _SELF_FACTS:
        match = pattern.search(body)
        if match is None:
            continue
        value = str(match.group("value") or "").strip(" 。，,.;；:：")
        if not value:
            # `city` 那条有第二个分支（句尾短词），命中它时 `value` 是 None。
            try:
                value = str(match.group("value2") or "").strip(" 。，,.;；:：")
            except (IndexError, re.error):
                value = ""
        if _too_odd(value):
            continue
        # 问句不是陈述。这一关放在抽到值**之后**：一个问句里同样有「我的网站」四个字。
        if _NOT_A_STATEMENT.search(body) and not standing:
            continue
        statement = f"{label}：{value}"
        if not MIN_STATEMENT <= len(statement) <= MAX_STATEMENT:
            continue
        # **一条第一人称自述本身就是显式陈述** —— 这是 `explicit` 那条通道，不需要使用者
        # 再说一句「记住」。使用者的原话正是这个：「而不是我说了『给我记住我的个人网站』
        # 他才会记住」。纠正比自述更强，所以它盖在上面。
        channel = CORRECTION if correcting else EXPLICIT
        found.setdefault(
            key,
            Candidate(key=key, statement=statement, kind="user_preference", channel=channel),
        )

    if standing and not found:
        # 「以后都用中文回我」—— 一条长期指令，但不匹配任何具体字段。整句留下来，
        # 因为它就是可复述的那一句。
        cleaned = body.strip(" 。，,.！!")
        if MIN_STATEMENT <= len(cleaned) <= MAX_STATEMENT:
            found["standing"] = Candidate(
                key="standing",
                statement=cleaned,
                kind="project_rule",
                channel=EXPLICIT,
            )
    return list(found.values())


@dataclass
class MemoryPromoter:
    """把候选记下来，够证据的直接进长期层。

    挂在 runtime 上、每轮结束后调一次 ``observe()``。**吞掉自己的异常** —— 记忆是增强不是
    对话的前提，这条和 ``_recall_context`` 同一个立场。
    """

    writer: Any
    recaller: Any = None
    store: Any = None
    #: 同一条候选在几个不同会话里出现过就晋升。2 是「不止一次」的最小值。
    repeat_sessions: int = 2
    promoted: int = 0
    parked: int = 0
    last_error: str = ""

    def observe(self, text: str, *, session_id: str | None = None) -> list[Candidate]:
        """看一轮的用户输入。返回**这一轮真的晋升了**的那些。"""
        try:
            candidates = extract(text)
        except Exception as exc:  # noqa: BLE001 - 抽取失败不该影响这一轮
            self.last_error = f"抽取失败：{type(exc).__name__}: {exc}"
            return []
        promoted: list[Candidate] = []
        for candidate in candidates:
            try:
                decided = self._decide(candidate, session_id=session_id)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"晋升失败：{type(exc).__name__}: {exc}"
                continue
            if decided is not None:
                promoted.append(decided)
        return promoted

    def _decide(self, candidate: Candidate, *, session_id: str | None) -> Candidate | None:
        channel = candidate.channel
        if not channel:
            channel = self._repeat_channel(candidate, session_id=session_id)
        if not channel:
            self._park(candidate, session_id=session_id)
            return None
        if self._already_known(candidate):
            # 已经记着了就不再写一条。重复的事实会让召回时同一句话出现三遍，
            # 而那三遍会挤掉别的记忆。
            return None
        # tag 里带**通道**和 key：「这条记忆凭什么在这里」要能回答，
        # 而一条无从追溯的长期记忆是没法审计也没法回滚的。
        self.writer.write_fact(
            candidate.statement,
            tags=(f"channel:{channel}", f"about:{candidate.key}", f"kind:{candidate.kind}"),
            session_id=session_id,
        )
        self.promoted += 1
        return Candidate(
            key=candidate.key,
            statement=candidate.statement,
            kind=candidate.kind,
            channel=channel,
        )

    def _park(self, candidate: Candidate, *, session_id: str | None) -> None:
        """证据不够：留在候选层等下一次。"""
        write = getattr(self.writer, "write_candidate", None)
        if callable(write):
            write(candidate.statement, key=candidate.key, session_id=session_id)
        else:
            # 没有专用入口时借短期层，kind 不同所以不会被 `recent_turns` 当对话读回去。
            writer_write = getattr(self.writer, "_write", None)
            if callable(writer_write):
                writer_write(
                    scope="short",
                    kind=CANDIDATE_KIND,
                    text=candidate.statement,
                    session_id=session_id,
                    tags=(f"about:{candidate.key}",),
                )
        self.parked += 1

    def _repeat_channel(self, candidate: Candidate, *, session_id: str | None) -> str:
        """同一条候选在别的会话里出现过吗。"""
        store = self.store or getattr(self.writer, "store", None)
        lister = getattr(store, "list_records", None)
        if not callable(lister):
            return ""
        try:
            rows = lister(scope="short", kind=CANDIDATE_KIND, limit=200)
        except Exception:  # noqa: BLE001
            return ""
        sessions = {
            str(getattr(row, "session_id", "") or "")
            for row in rows
            if str(getattr(row, "text", "")) == candidate.statement
        }
        sessions.add(str(session_id or ""))
        return REPEATED if len(sessions) >= self.repeat_sessions else ""

    def _already_known(self, candidate: Candidate) -> bool:
        if self.recaller is None:
            return False
        try:
            hits = self.recaller.facts(candidate.statement, limit=5)
        except Exception:  # noqa: BLE001
            return False
        return any(str(getattr(hit, "text", "")) == candidate.statement for hit in hits)


__all__ = [
    "CANDIDATE_KIND",
    "CORRECTION",
    "EXPLICIT",
    "MAX_STATEMENT",
    "MIN_STATEMENT",
    "REPEATED",
    "Candidate",
    "MemoryPromoter",
    "extract",
]
