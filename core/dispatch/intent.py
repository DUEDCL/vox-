"""Rule-based intent classification (ADR 005, tool vs agent decision).

Keyword and regex matching routes「读一下 X」「搜一下 Y」「运行 Z」straight
to a local tool and executes it directly — millisecond latency instead of
seconds, and no agent involved. Only unmatched utterances go to agent routing.

This is deliberately **not a model**. A classifier model would add latency to
the fast path it is supposed to accelerate, add a model to the dependency set,
and cannot be tested deterministically. Rule hits are fully AUTO-testable.

**Every pattern is anchored at the start of the utterance** (``\\A``). An
unanchored ``search()`` is what turned「你能看看这个问题吗」into ``fs.read``
and「跑得动吗」into ``shell.run``: the verb appeared *somewhere*, so the rule
fired. A command the user never asked for is the worst outcome in this file, so
the rule now has to match how the request opens, not merely occur inside it.

Captures are greedy and validated by ``_plausible``. A lazy ``(.+?)`` matched a
single character, so「我想显示器换一个」yielded ``path="器"`` -- a hit that is
both wrong and unfalsifiable, because one character is a plausible-looking path.

**A verb must be followed by a boundary** (``_VERB_SEP``). Anchoring stops a verb
found in the middle of a sentence; it does nothing about one glued to the next
character.「运行时报错了怎么办」*opens* with「运行」and would have run「时报错
了怎么办」as a shell command;「搜索引擎是怎么工作的」is the same shape. 运行时
and 搜索引擎 are single words, and a separator is what tells them from a verb
plus its argument.

Pattern order: ``web.search`` tests **before** ``fs.read``, because
「读一下网页 X」contains both「读」and「网页」but means search. The previous
order claimed this in a comment and then did the opposite.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from core.dispatch.contract import Intent

#: Shortest captured argument that is allowed to trigger a tool. One character
#: is never a path, a query, or a command -- it is a lazy quantifier's leftovers.
MIN_ARGUMENT_LEN = 2

#: Openers that carry no meaning of their own.「请帮我搜一下 X」is the same
#: request as「搜一下 X」, and anchoring without this would miss the way people
#: actually speak. Granted **only** to unambiguous verbs below: allowing it on
#: 「查看」would turn「请帮我查看这个问题」into a file read.
#: Repeated, because「请帮我搜一下」stacks two of them and one is not enough.
#: Bounded to three so the regex cannot walk an arbitrary prefix.
#:
#: **2026-09-03 加了「给我」这一组。** 使用者说的是「给我打开网易云音乐」，而这个表里
#: 只有「帮我」—— 于是整句锚定在第一个字就失配，意图落到 agent 上，agent 回了一句
#: 「好，正在打开网易云音乐。」**然后什么都没发生**（它没有那个工具）。日志里那一行是
#: `intent 试了给我打开网易云音乐 -> agent`。少一个礼貌前缀的代价不是少一种说法，
#: 是这条工具路径在他的口语里根本不存在。
_POLITE = (
    r"(?:(?:请|請|麻烦|麻煩|帮我|幫我|帮忙|幫忙|给我|給我|替我|为我|為我|能不能|可以|"
    r"please|can\s+you)\s*){0,3}"
)

#: What must sit between a Chinese verb and its argument: either a marker the
#: verb takes (「一下」「命令」) or real whitespace.
#:
#: Anchoring fixed「动词出现在句中」;this fixes「动词粘着下一个字」. 「运行时报
#: 错了怎么办」**opens** with「运行」, so the anchor let it through, and the
#: capture ran「时报错了怎么办」as a shell command -- 运行时 is one word.
#: 「搜索引擎是怎么工作的」is the same shape. Requiring a boundary is what tells
#: 「运行 pytest」from「运行时」, and it costs only the glued forms
#: (「查看README.md」), which fall through to the agent -- the safe direction.
_VERB_SEP = r"(?:\s*(?:一?下|命令)\s*|\s+)"

#: Patterns for ``web.search``. Tested **first**: 「读一下网页 X」opens with
#: 「读」but means search, and the web marker is the more specific signal.
_WEB_SEARCH_PATTERNS = (
    rf"\A{_POLITE}(?:读一?下|看一?下|查一?下)\s*(?:网页|網頁|网站|網站)\s*(.+)",
    rf"\A{_POLITE}搜{_VERB_SEP}(.+)",
    rf"\A{_POLITE}(?:搜索|搜尋|查找){_VERB_SEP}(.+)",
    rf"\A{_POLITE}网上\s*(?:找|搜|查)\s*(.+)",
    rf"\A{_POLITE}(?:search|find|look\s+up)\s+(?:for\s+)?(.+)",
    rf"\A{_POLITE}(?:google|bing|duckduckgo)\s+(.+)",
)

#: Patterns for ``fs.read``. ``打开`` is deliberately not here: it is ambiguous
#: between files and URLs. Bare「看」is not here either -- 「看看这个问题」and
#: 「帮我看看日程」are conversation, not a file read.
_FS_READ_PATTERNS = (
    rf"\A{_POLITE}读{_VERB_SEP}(.+?)(?:这个|這個)?(?:文件|檔案)?\Z",
    # 「查看」/「显示」get no polite prefix: they are the ambiguous pair.
    rf"\A(?:查看|显示|顯示){_VERB_SEP}(.+?)(?:的)?(?:文件|檔案|内容|內容)?\Z",
    r"\Acat\s+(.+)",
    rf"\A{_POLITE}(?:read|show|display)\s+(?:the\s+)?file\s+(.+)",
    # "read the README file" -- the noun comes before ``file`` in English too.
    rf"\A{_POLITE}(?:read|show|display)\s+(?:the\s+)?(.+?)\s+file\s*\Z",
    rf"\A{_POLITE}(?:read|open)\s+(\S+\.\w+)\s*\Z",
)

#: Patterns for ``shell.run``. Deliberately the most conservative set: an FP
#: here means executing a command the user did not intend, which is worse than
#: an FN (the agent would still answer).
_SHELL_RUN_PATTERNS = (
    rf"\A{_POLITE}(?:运行|執行|执行|運行|跑){_VERB_SEP}(.+)",
    rf"\A{_POLITE}(?:run|execute|exec)\s+(?:the\s+)?(?:command\s+)?(.+)",
    # git/npm/cargo at the **start** also count, because「git status」is
    # unambiguous -- but "I read that git is hard" is not, and anchoring is
    # what tells them apart. No polite prefix: a bare command is already the
    # whole utterance.
    r"\A(git\s+\S+.*)",
    r"\A(npm\s+\S+.*)",
    r"\A(cargo\s+\S+.*)",
)

#: Utterances that end in a question particle are asking, not commanding.
#: 「跑得动吗」matched ``跑`` and would have run「得动吁」as a shell command.
_QUESTION_TAIL = re.compile(r"(?:吗|嗎|呢|吧)\s*[?？]?\s*\Z")

#: ``time.now``。整句匹配、不捕获参数 —— 时区是本机的属性，「现在几点」没有参数可提。
#:
#: 这一组敢用整句锚定是因为这些说法没有第二个意思：「几点了」不会是别的请求。而它们
#: 派给 agent 要几秒和一次出网，答案却在本机时钟里。
_TIME_NOW_PATTERNS = (
    r"\A(?:现在|現在|目前|当前|當前)?\s*(?:是)?\s*(?:几点|幾點|什么时候|什麼時候|时间|時間)"
    r"(?:了|钟|鐘)?\s*[?？。]?\s*\Z",
    r"\A(?:几点|幾點)(?:了|钟|鐘)?\s*[?？。]?\s*\Z",
    r"\A(?:今天|今日)\s*(?:是)?\s*(?:几号|幾號|星期几|星期幾|周几|週幾|礼拜几|禮拜幾)"
    r"\s*[?？。]?\s*\Z",
    r"\A(?:报时|報時|报一下时间|報一下時間)\s*[?？。]?\s*\Z",
    r"\A(?:what(?:'s| is)\s+the\s+time|what\s+time\s+is\s+it)\s*[?.]?\s*\Z",
)

#: ``app.open``：打开一个本机应用。
#:
#: 名字必须**短且不含分隔符**（见 ``_looks_like_app_name``）—— 「打开」对文件、网址和应用
#: 三样都歧义，而形状是唯一能在不碰文件系统、不读工具配置的前提下分开它们的东西。
#: 白名单不在这一层：意图层不该知道装了什么，工具拒绝时会列出能开的，那是有信息的失败。
_APP_OPEN_PATTERNS = (
    rf"\A{_POLITE}(?:打开|打開|启动|啟動|开启|開啟){_VERB_SEP}?(.+?)"
    r"(?:吧|呀)?\s*[。!！]?\s*\Z",
    rf"\A{_POLITE}(?:open|launch|start)\s+(.+?)\s*[.!]?\s*\Z",
)

#: 泛指的「放点音乐」—— 没有具体曲目，所以是「打开那个播放器」而不是「搜这首歌」。
#: 单独一组是因为它**不捕获参数**：要开哪个由 ``apps.default_music`` 定。
_PLAY_ANY_PATTERNS = (
    r"\A(?:放|播放|听|聽|来|來)\s*(?:点|點|一?首|一?些)?\s*(?:音乐|音樂|歌|歌曲|歌儿|歌兒)"
    r"\s*(?:吧|呀|听|聽)?\s*[。!！?？]?\s*\Z",
    r"\A(?:play|put\s+on)\s+(?:some\s+)?music\s*[.!]?\s*\Z",
)

#: ``play.song``：**点名了要听谁 / 听什么**，所以它既要开播放器，又要把这个词交给它。
#:
#: 与 ``play.any`` 的分工在于捕不捕获参数：「放点音乐」开默认播放器就完了，「我想听薛之谦
#: 的歌」不开到那个人的歌就等于没做这件事。使用者的原话：「比如我说『我想听薛之谦的歌』
#: 就会打开默认音乐播放器并播放薛之谦的歌」。
#:
#: **必须带「歌 / 音乐 / 曲子 / 专辑」这一类标记词**（或者「一首 / 一张」）。少了它
#: 「我想听你的意见」「听我说」都会命中，而那两句要的是对话不是播放器。标记词是这条规则
#: 唯一的边界 —— 「听」这个动词本身对播放和倾听两义。
_PLAY_SONG_PATTERNS = (
    # 我想听薛之谦的歌 / 听薛之谦的歌 / 放周杰伦的音乐 / 来点李荣浩的歌
    rf"\A{_POLITE}(?:我)?(?:想|要|想要)?\s*(?:听|聽|放|播放|来点|來點|来些|來些)\s*"
    r"(.+?)(?:的)?(?:歌|歌曲|音乐|音樂|曲子|专辑|專輯)(?:吧|呀|了|嘛)?\s*[。!！]?\s*\Z",
    # 放一首稻香 / 来一首薛之谦
    rf"\A{_POLITE}(?:我)?(?:想|要|想要)?\s*(?:听|聽|放|播放|来|來)\s*(?:一)?(?:首|张|張)\s*"
    r"(.+?)(?:吧|呀)?\s*[。!！]?\s*\Z",
)

#: ``web.open``：把一个地址或一次搜索交给浏览器。
#:
#: 「播放周杰伦的稻香」落在这里而不是 ``web.search``：后者把结果抓回来给平台读，而这句话
#: 要的是一个能点播放的页面 —— 渲染它的是浏览器，不是我们。
_WEB_OPEN_PATTERNS = (
    rf"\A{_POLITE}(?:打开|打開|访问|訪問|上)\s*((?:https?://|www\.)\S+)\s*\Z",
    rf"\A{_POLITE}(?:播放|放|听|聽){_VERB_SEP}?(.+?)(?:吧|呀)?\s*[。!！]?\s*\Z",
    rf"\A{_POLITE}(?:play|open)\s+((?:https?://|www\.)\S+)\s*\Z",
)


#: 「使用者点名了一个动词」的那些词。只给 ``weather.now`` 的否定前视用。
#:
#: 它们各自已经有自己的规则（`web.search` / `fs.read` / `app.open` / `shell.run`），
#: 而**显式动词赢**：说「搜一下武汉天气」的人点名了要搜网页，即便这个平台有一个更准的
#: 天气工具。挡在正则开头而不是靠规则顺序，是因为顺序只解决带空格的那一种 —— 粘着写的
#: 「搜一下武汉天气」会让城市变成「搜一下武汉」，然后报一句「没找到叫『搜一下武汉』的地方」。
_REQUEST_VERBS = (
    r"(?:请|請|麻烦|麻煩|帮我|幫我|帮忙|幫忙|给我|給我|替我|为我|為我|能不能|可以|"
    r"please|can\s+you|搜|查|找|读|讀|看|打开|打開|启动|啟動|运行|運行|执行|執行|"
    r"播放|放|听|聽|显示|顯示|告诉|告訴)"
)

#: ``weather.now``：「今天天气怎么样」。
#:
#: **这一句最该走快路径**，因为它是被问得最多的一句，而派给 agent 要多一次 LLM 往返
#: （实测 1.8–4 秒）换回来的只是同一个工具调用。
#:
#: **标记词是「天气」/「气温」/「下雨」/「下雪」，而且必须整句锚定** —— 「天气」这个词在
#: 别的句子里出现时多半是在谈论它而不是在问它（「天气这个词怎么写」「天气预报那个节目」），
#: 而那两句都带着后缀，整句锚定就把它们排除了。
#:
#: **开头那个否定前视是必需的，它修的是一次真实的冲突。** 「搜一下 武汉天气」在测试里
#: 是一条 ``web.search``（有它自己的理由：使用者点名了动词），而城市捕获组会把「搜一下 武汉」
#: 整段吃进去。显式动词赢 —— 所以这里先把 `_REQUEST_VERBS` 挡在外面，而不是靠
#: `web.search` 排在前面这一条顺序（顺序解决带空格那一种，挡不住粘着写的「搜一下武汉天气」，
#: 那时城市会变成「搜一下武汉」，然后地名解析报「没找到叫『搜一下武汉』的地方」）。
#:
#: 城市捕获只收**汉字与字母**（不含空白与标点），时间词在 ``_extract`` 里剥掉
#: （「今天上海天气」→ 上海）。捕不到城市时 ``city`` 是空串，工具用默认城市 —— 那正是
#: 「今天天气怎么样」该走的路。
_WEATHER_PATTERNS = (
    # [今天/明天][上海]天气[怎么样/如何/好不好]
    rf"\A(?!{_REQUEST_VERBS})(?:今天|今日|明天|明日|现在|現在|当前|當前)?\s*"
    r"([一-鿿A-Za-z]{0,8}?)\s*"
    r"(?:的)?\s*(?:天气|天氣|气温|氣溫)\s*"
    r"(?:预报|預報)?\s*(?:怎么样|怎麼樣|咋样|咋樣|如何|好不好|怎样|怎樣|多少度)?"
    r"\s*[?？。!！]?\s*\Z",
    # [明天][北京]会下雨 —— `_QUESTION_TAIL` 会先把带「吗」的整句拦成 agent，所以真正
    # 落到这里的是「明天下雨」「今天会下雨」这类不带语气词的说法。
    rf"\A(?!{_REQUEST_VERBS})(?:今天|今日|明天|明日)?\s*"
    r"([一-鿿A-Za-z]{0,8}?)\s*(?:会|會)?\s*"
    r"(?:下雨|下雪|降雨|降雪)\s*[?？。!！]?\s*\Z",
    r"\A(?:what(?:'s| is)\s+the\s+weather)(?:\s+in\s+(.+?))?\s*[?.]?\s*\Z",
)

#: 「今天 / 明天」这些词不是城市名。城市捕获组会把它们连着城市一起吃进去
#: （「上海明天天气」→ ``上海明天``），所以在 ``_extract`` 里剥掉。
_WHEN_WORDS = ("今天", "今日", "明天", "明日", "现在", "現在", "当前", "當前", "的", "会", "會")

#: 哪些词表示「明天」。剥城市名之前先看一眼原句 —— 剥完就看不出来了。
_TOMORROW_MARKS = ("明天", "明日")


def _weather_arguments(sentence: str, captured: str) -> dict[str, Any]:
    """「今天上海天气怎么样」→ ``{"city": "上海", "day": "today"}``。

    城市捕获组是贪心之外的一段任意文本，所以它会把时间词、「的」「会」这类虚词连着城市
    一起吃进去。剥掉它们**只在两端做**（`strip`），不在中间删字：「西安」里没有虚词，
    而一个会在中间删字的清理器会把地名改成另一个地名，那正是这个工具最不能出的错。

    剥完只剩一个字或什么都不剩时报空城市 —— 那时工具用默认城市，比拿「天」去查一个
    不存在的地方好。
    """
    day = "tomorrow" if any(mark in sentence for mark in _TOMORROW_MARKS) else "today"
    city = captured.strip()
    changed = True
    while changed and city:
        changed = False
        for word in _WHEN_WORDS:
            for stripped in (city.removeprefix(word), city.removesuffix(word)):
                if stripped != city:
                    city, changed = stripped.strip(), True
                    break
            if changed:
                break
    return {"city": city if len(city) >= 2 else "", "day": day}


#: 不捕获参数的规则。``_plausible`` 对它们放行 —— 「几点了」没有参数可提，而一个要求
#: 参数非空的检查会把这一整类拒掉。
#:
#: ``weather.now`` 在里面是因为**空城市是它的正常输入**：「今天天气怎么样」捕不到城市，
#: 那时该用默认城市，而不是让整条规则失配掉到 agent 上（那就白花了一次 LLM 往返）。
#: 它的 ``_extract`` 因此**不走** ``_NO_ARGUMENT_RULES`` 那条早退 —— 它有参数，只是允许为空。
_NO_ARGUMENT_RULES = frozenset({"time.now", "play.any", "weather.now"})

#: 允许**单字**参数的规则。
#:
#: `MIN_ARGUMENT_LEN` 存在的理由是「一个字不可能是路径 / 查询 / 命令」，那条对
#: `fs.read` / `shell.run` 成立。可是**歌名可以只有一个字** —— 2026-09-03 使用者说
#: 「给我放首雪」，捕获出「雪」，被这条长度检查扔掉，于是整句漏给了 `web.open`
#: （它捕获的是「首雪」，一个不存在的歌名），最后落到 agent，agent 回了一句
#: 「好，搜歌名『雪』给你放上。」**然后什么都没发生。**
#:
#: 放宽只对这一条规则：判错的代价是「播放器里搜了一个字」，而不是「跑了一条命令」。
_SHORT_ARGUMENT_RULES = frozenset({"play.song"})

#: 规则名 -> 真实工具名。``play.any``（「放点音乐」）是 ``app.open`` 的一种说法：它不带
#: 名字，开哪个由 ``apps.default_music`` 定。分成两条规则而不是一条，是因为**捕不捕获
#: 参数**不同 —— 合成一条会让「放音乐」和「播放稻香」走同一个提取逻辑，而后者要的是搜索。
#:
#: ``play.song`` 也落到 ``app.open``：开哪个播放器仍然由 ``apps.default_music`` 定，
#: 多出来的只是一个 ``query``。让它去 ``web.search`` 是另一件事 —— 那会把搜索结果**抓回来
#: 念给人听**，而使用者要的是那首歌响起来。
_RULE_TO_TOOL = {"play.any": "app.open", "play.song": "app.open"}


#: 需要一个**真能动这台机器**的后端的说法。命中它们的一句话带上 ``code`` 能力去路由，
#: 于是裸 HTTP 端点（``relay``，只声明 ``chat`` / ``reason``）被闸门挡掉，剩下的是
#: ``claude`` 这类本机 CLI。
#:
#: ## 为什么必须有这一层
#:
#: 5 维评分里 ``cost`` 与 ``latency`` 占 70%，而裸端点在这两项上必然赢过一个要起进程的
#: CLI（实测 relay 0.866 / claude 0.602）。能力在评分里是**闸门**，可是 ``Task`` 从来没有
#: 人给它填 ``capabilities`` —— 空集是任何集合的子集，于是闸门在生产里根本不生效，
#: 「帮我改一下这个函数」和「今天天气怎么样」走同一个后端，而前者那个后端连文件都读不了。
#: 见 `docs/adr/008-vox-as-primary-brain.md` 与 `config/agents.toml` 的开头。
#:
#: ## 判错的代价是不对称的，所以这一组比工具规则宽
#:
#: 工具规则误判 = **运行一条用户没要求的命令**，所以那边处处锚定、处处要边界。这里误判
#: 只是「换了个更能干、更慢的后端答同一句话」—— 代价是几秒，不是一次副作用。所以这里
#: 用 ``search()`` 而不是 ``\\A``：「这个函数你帮我改一下」的动词在句尾，而它确实是要
#: 改代码。反方向（漏判）才是真损失：一句「跑一下测试」落到裸端点上，换回来的是一段
#: 它其实执行不了的说明。
_CODE_PATTERNS = (
    # 写/改/修/重构 + 代码物件。物件词是必需的：「写一封邮件」不该起 CLI。
    r"(?:写|寫|改|修|重构|重構|优化|優化|实现|實現|加|删|刪)\s*(?:一?下|个|個|一?点)?\s*"
    r"[^。！？\n]{0,12}?(?:代码|代碼|脚本|腳本|程序|函数|函數|方法|类|類|接口|模块|模組|"
    r"测试|測試|配置文件|bug|BUG)",
    # 项目/仓库/文件系统里的活。
    r"(?:项目|項目|仓库|倉庫|代码库|代碼庫|工程)\s*(?:里|裡|中|的)",
    # 中文侧不带 ``\b``：Python 的 ``\b`` 在两个汉字之间不成立（都是 word 字符），
    # 于是「提交一下」根本匹配不上 —— 这是 CJK 上用 ``\b`` 的经典失败。
    r"(?:提交|推上去|部署|构建|構建|编译|編譯)",
    r"\b(?:commit|push|deploy|build|compile)\b",
    r"(?:跑|运行|執行|执行|運行)\s*(?:一?下)?\s*(?:测试|測試|test|pytest|npm|cargo|构建|build)",
    r"(?:报错|報錯|编译不过|編譯不過|测试不过|測試不過|跑不起来|跑不起來|调试|調試|debug)",
    # 英文侧。``\b`` 够用：英文有空格边界。
    r"\b(?:refactor|implement|debug|fix)\b.{0,24}\b(?:code|script|function|class|test|bug|file)s?\b",
    r"\b(?:write|create|add)\b.{0,24}\b(?:script|function|class|test|module|program)s?\b",
    r"\bgit\s+\w+",
)

#: 「这句话要一个能动机器的后端」用哪个能力词表达。
#:
#: ``code`` 而不是 ``local-exec``：两个词在出厂配置里都只有本机 CLI 有，但 ``code`` 是
#: 三个 CLI 都声明了的那个，而 ``local-exec`` 的语义是「能读写这台机器」——
#: 将来会有只写代码不动机器的后端，那时这两个词要能分开。
CODE_CAPABILITY = "code"

_CODE_RULES = tuple(re.compile(pattern, re.IGNORECASE | re.UNICODE) for pattern in _CODE_PATTERNS)


def required_capabilities(text: str) -> frozenset[str]:
    """这一句话要求后端具备什么。普通对话返回空集（任何后端都行）。

    纯函数、无状态、不碰文件系统 —— 和这个模块里其余部分同一个姿态。
    """
    stripped = text.strip()
    if not stripped:
        return frozenset()
    for rule in _CODE_RULES:
        if rule.search(stripped):
            return frozenset({CODE_CAPABILITY})
    return frozenset()


#: 起头的应答词。「好，没事了」和「没事了」是同一句话，而 ASR 出来的正是前一种 ——
#: 人说话会先应一声再说内容。上限两个，正则不许往前走任意长的前缀。
_LEAD_IN = r"(?:(?:好的?|嗯+|那|行|成|ok|okay)\s*[,，。、]?\s*){0,2}"

#: 「这次聊完了」的说法。命中它的一句话**不派给任何后端**，直接收尾 ——
#: 见 ``vox_plugin/runtime.py`` 的 ``_dismiss``。
#:
#: ## 为什么比工具规则还严
#:
#: 判错的代价在这里是反过来的。工具规则怕的是误判（跑了一条没人要的命令），这里怕的
#: 同样是误判，但形状不同：**对话在人还要说下去的时候被挂掉**。所以每一条都整句锚定
#: （``\A…\Z``），「这个不用了，用那个」不会命中（前后都还有内容），而单独一句
#: 「不用了」会。漏判的代价只是助手把这句话当话题答一句 —— 傻，但无害。
#:
#: ## 「结束」「停止」「退出」必须带对象
#:
#: 裸的「结束」在这个产品里有第二个意思：结束正在跑的那件事。所以只收「结束对话」
#: 「退出会话」这种带对象的说法 ——「帮我结束这个进程」不该把电话挂掉。
#:
#: ## 不要拿 ``_QUESTION_TAIL`` 当前置守卫
#:
#: 那个正则把「吧」算作问句尾，而「退下吧」「就这样吧」的「吧」是软化语气不是提问。
#: 用它过一遍会把这一整类静默拒掉，症状是「说了退下吧它还在聊」，而且没有任何报错。
_DISMISS_PATTERNS = (
    # 退下 / 你先退下 / 退下吧。
    r"(?:你|您)?\s*(?:先|可以|就)?\s*(?:退下|下去)(?:吧|了|啦)*",
    # 你可以走了 / 去休息吧。这几个动词**必须**带完成语气词，否则「走这条路」
    # 「睡不着怎么办」会擦到。
    r"(?:你|您)?\s*(?:可以|先|去)?\s*(?:走|睡|休息|歇会儿|歇會兒)(?:吧|了|啦)+",
    # 结束 + 对象。对象是必需的，理由见上面。
    r"(?:结束|結束|停止|退出|关闭|關閉|终止|終止)\s*(?:本次|这次|這次|当前|當前)?\s*(?:的)?"
    r"(?:对话|對話|会话|會話|聊天|交流)(?:吧|了|啦)*",
    # 对话到此为止 / 聊天就先这样。
    r"(?:对话|對話|会话|會話|聊天)\s*(?:就)?\s*"
    r"(?:结束|結束|到此为止|到此為止|先这样|先這樣|够了|夠了)(?:吧|了|啦)*",
    # 没事了 / 不用了 / 没别的了 / 不聊了。语气词是必需的：裸的「不用」是在回答问题。
    r"(?:就)?\s*(?:没事|沒事|没别的事|沒別的事|没别的|沒別的|不用|不聊|不问|不問|"
    r"没有了|沒有了|没啥事|沒啥事)(?:了|吧|啦)+",
    # 就这样吧 / 到这就行了 / 先这样。
    r"(?:就|先)?\s*(?:这样|這樣|到这|到這|到此)\s*(?:就好|就行|吧|了|啦)+",
    r"(?:先|就)(?:这样|這樣)",
    # 告别语。这些没有第二个意思。
    r"(?:再见|再見|拜拜|拜了|晚安|goodbye|good\s*bye|bye(?:\s*bye)?|see\s+you)(?:了|啦|吧)*",
    # 英文侧。
    r"(?:that'?s\s+(?:all|it)|nothing\s+else|never\s+mind|we'?re\s+done|i'?m\s+done|dismissed)",
)

#: 整句锚定 + 允许尾部标点（打字进来的那条路会带标点，ASR 那条路不会）。
_DISMISS_RULES = tuple(
    re.compile(
        rf"\A{_LEAD_IN}{_POLITE}(?:{body})\s*[。．.!！?？~…、,，]*\Z",
        re.IGNORECASE | re.UNICODE,
    )
    for body in _DISMISS_PATTERNS
)


def is_dismissal(text: str) -> bool:
    """这一句是「聊完了」而不是一个请求。

    纯函数，和 ``required_capabilities`` 同一个姿态：不碰状态、不碰文件系统、不看配置。
    """
    stripped = text.strip()
    if not stripped:
        return False
    return any(rule.match(stripped) for rule in _DISMISS_RULES)


#: 「进度怎么样了」这一类 —— 使用者在问**现在手上那件事**做到哪了，不是在提一个新请求。
#:
#: 存在的理由：一次派给 `claude` 的活可能跑十几秒到几分钟，而朗读期间现在可以打断了
#: （见 `capture.duck_for`）。于是「打断 → 问进度」成了一条真实的路径，而它**必须由本机
#: 回答** —— 把「你在干什么」发给云端 agent 是荒谬的：它不知道，而且答它还要再等一轮。
#:
#: 边界比 `is_dismissal` 更要紧，因为这几个词的日常用法多：
#: 「这个项目进度怎么样」是在问一个**项目**，得派给 agent；「你进度怎么样」问的是助手自己。
#: 所以带主语的那些只认第二人称，不带主语的（「进度呢」「还要多久」）才无条件收。
_PROGRESS_PATTERNS = (
    # 你/现在 + 进度/在干什么/做到哪了
    r"(?:你|您|现在|目前)?\s*(?:的)?\s*(?:进度|進度|进展|進展)\s*(?:怎么样|怎麼樣|如何|呢|了)*",
    r"(?:你|您)\s*(?:现在|目前)?\s*(?:在)?(?:干|做|忙)\s*(?:什么|啥|什麼)\s*(?:呢|了)*",
    r"(?:做|跑|处理|處理)\s*(?:到|得)\s*(?:哪|怎么样|怎麼樣)\s*(?:了|儿|兒)*",
    r"(?:还要|還要|还得|還得|要)\s*(?:等)?\s*(?:多久|多长时间|多長時間)\s*(?:呢|啊)*",
    r"(?:好了吗|好了沒|好了没|完成了吗|完成了嗎|弄好了吗|弄好了嗎|搞定了吗|搞定了嗎)",
    r"(?:什么情况|什麼情況|怎么样了|怎麼樣了)",
    # 英文侧
    r"(?:status|progress|any\s+update|how(?:'?s|\s+is)\s+it\s+going|are\s+you\s+done|what\s+are\s+you\s+doing)\s*\??",
)

_PROGRESS_RULES = tuple(
    re.compile(
        rf"\A{_LEAD_IN}{_POLITE}(?:{body})\s*[。．.!！?？~…、,，]*\Z",
        re.IGNORECASE | re.UNICODE,
    )
    for body in _PROGRESS_PATTERNS
)


def is_progress_query(text: str) -> bool:
    """这一句是「你现在做到哪了」而不是一个新请求。

    纯函数，和 ``is_dismissal`` 同一个姿态。**整句锚定**：「这个项目的进度怎么样」不匹配
    （它带了一个宾语，要问的是那个项目），而「进度怎么样了」匹配。
    """
    stripped = text.strip()
    if not stripped:
        return False
    return any(rule.match(stripped) for rule in _PROGRESS_RULES)


class RuleBasedIntentResolver:
    """Keyword and regex matching, with the patterns above baked in.

    A custom instance can override ``patterns`` to add project-specific
    shortcuts, but the shipped set is what most users will see.
    """

    def __init__(
        self,
        *,
        patterns: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.patterns = dict(patterns) if patterns is not None else {
            "time.now": _TIME_NOW_PATTERNS,
            "weather.now": _WEATHER_PATTERNS,
            "play.any": _PLAY_ANY_PATTERNS,
            "play.song": _PLAY_SONG_PATTERNS,
            "web.search": _WEB_SEARCH_PATTERNS,
            "web.open": _WEB_OPEN_PATTERNS,
            "fs.read": _FS_READ_PATTERNS,
            "app.open": _APP_OPEN_PATTERNS,
            "shell.run": _SHELL_RUN_PATTERNS,
        }
        # Compile once rather than per utterance, and keep order.
        self._compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
        for tool, raw_patterns in self.patterns.items():
            self._compiled[tool] = tuple(
                re.compile(pat, re.IGNORECASE | re.UNICODE) for pat in raw_patterns
            )

    def resolve(self, text: str) -> Intent:
        """Classify without a model call; fall back to ``kind="agent"``.

        The returned ``arguments`` holds the extracted portion (the path, the
        query, the command). The caller still validates and gates it.

        Falling through to ``agent`` is the safe direction and the common one.
        A missed rule costs a few seconds; a false one runs a command.
        """
        stripped = text.strip()
        if not stripped:
            return Intent(kind="agent", confidence=0.0)
        # A question is not an instruction.「跑得动吗」asks whether something
        # runs; it does not ask to run「得动吗」.
        if _QUESTION_TAIL.search(stripped):
            return Intent(kind="agent", confidence=0.0)
        # Order matters: ``web.search`` runs before ``fs.read``, because
        # 「读一下网页 X」contains both「读」and「网页」but means search.
        #
        # 新加的四组按「谁更具体」排：``time.now`` 和 ``play.any`` 是整句匹配、不捕获参数，
        # 不可能误伤别的请求，所以最先；``web.open`` 在 ``fs.read`` 之前，因为「打开
        # https://…」两边都能匹配而它是网址；``app.open`` 最后，它捕获的是「打开」后面
        # 剩下的任何东西，是这一串里最宽的一条。
        for tool in (
            "time.now",
            "weather.now",
            "play.any",
            "play.song",
            "web.search",
            "web.open",
            "fs.read",
            "app.open",
            "shell.run",
        ):
            for pattern in self._compiled.get(tool, ()):
                match = pattern.search(stripped)
                if match is None:
                    continue
                arguments = self._extract(tool, match)
                if tool not in _NO_ARGUMENT_RULES and not self._plausible(
                    arguments, short_ok=tool in _SHORT_ARGUMENT_RULES
                ):
                    # A one-character capture is a quantifier artefact, not an
                    # argument. Keep looking, and fall through to the agent if
                    # nothing better matches.
                    continue
                return Intent(
                    kind="tool",
                    tool=_RULE_TO_TOOL.get(tool, tool),
                    arguments=arguments,
                    # Confidence is 1.0 for a hit, because the hit is the only
                    # evidence: there is no model score to average or threshold.
                    confidence=1.0,
                )
        return Intent(kind="agent", confidence=0.0)

    @staticmethod
    def _plausible(arguments: Mapping[str, Any], *, short_ok: bool = False) -> bool:
        """Reject captures too short to be a real path, query, or command.

        ``short_ok`` 是给歌名开的例外（见 ``_SHORT_ARGUMENT_RULES``）：一个字不可能是
        路径或命令，但完全可以是一首歌。
        """
        value = next(iter(arguments.values()), "")
        floor = 1 if short_ok else MIN_ARGUMENT_LEN
        return isinstance(value, str) and len(value.strip()) >= floor

    @staticmethod
    def capabilities(text: str) -> frozenset[str]:
        """这一句话要求后端具备什么。见 ``required_capabilities``。

        挂在解析器上而不是让派发器直接 import：解析器本来就是可注入的，把「这句话要
        什么后端」和「这句话是不是工具调用」放在同一个可替换对象上，换掉它就两件事一起
        换 —— 而两个判断用不同的文本理解方式是下一个 bug 的形状。
        """
        return required_capabilities(text)

    def _extract(self, tool: str, match: re.Match[str]) -> dict[str, Any]:
        """The named payload from the regex hit."""
        captured = match.group(1).strip() if match.lastindex else ""
        if tool == "weather.now":
            # **在 ``_NO_ARGUMENT_RULES`` 那条早退之前。** 它在那个集合里只是为了让
            # ``_plausible`` 放行空城市（「今天天气怎么样」该用默认城市，不该掉给 agent），
            # 但它确实有参数 —— 从那条早退里出来会把捕到的城市名扔掉。
            return _weather_arguments(match.string, captured)
        if tool in _NO_ARGUMENT_RULES:
            # 「几点了」「放点音乐」都没有参数：前者的时区是本机属性，后者开哪个由配置定。
            return {}
        if tool == "fs.read":
            path = _trim_path(captured)
            # 不像路径就**不认这个工具**，让它落到 agent 上。
            #
            # 实测过的形状：「读一下 一下」捕获出 ``一下``，然后 fs.read 以 ``no such file``
            # 收场，而用户在界面上看到的是「route=tool ok=false 0ms」—— 一个既不告诉他哪里
            # 错了、也不给他答案的回合。落到 agent 至少能得到一句话。
            return {"path": path} if _looks_like_path(path) else {}
        if tool == "web.search":
            return {"query": captured}
        if tool == "play.song":
            # 只带 ``query``，不带 ``name``：开哪个播放器由 ``apps.default_music`` 定，
            # 而这个词是要交给它去搜的东西。两个都填会让「打开哪个」和「搜什么」纠缠在
            # 一句话的两半上。
            return {"query": captured}
        if tool == "web.open":
            value = captured.strip()
            lowered = value.casefold()
            if lowered.startswith(("http://", "https://")):
                return {"url": value}
            if lowered.startswith("www."):
                # 说「上 www.bilibili.com」的人给的是主机名，补上协议是补一个他省略的字，
                # 不是替他改地址。https 而不是 http：降级到明文得是个显式选择。
                return {"url": "https://" + value}
            return {"query": value}
        if tool == "app.open":
            name = captured.strip()
            return {"name": name} if _looks_like_app_name(name) else {}
        if tool == "shell.run":
            return {"command": captured}
        return {}


#: 出现这些词就不是应用名。
#:
#: 指示代词和人称代词指的是上下文里的某个东西，而应用名是个固定的专有名词 —— 「打开这个
#: 问题看看」说的是别的事。动词性尾巴（「看看」「试试」）同理：应用名不带动作。
#: 这是个否定表而不是肯定表：肯定表要穷举装了什么，那正是意图层不该知道的。
_NOT_APP_WORDS = (
    "这个", "這個", "那个", "那個", "这些", "這些", "那些",
    "我的", "你的", "他的", "它的",
    "看看", "试试", "試試", "瞧瞧",
)


def _looks_like_app_name(text: str) -> bool:
    """像不像一个应用的名字：短、不含路径或通配字符、也不像个文件名。

    「打开」对文件、网址和应用三样都歧义，而形状是唯一能在**不碰文件系统、不读工具配置**
    的前提下把它们分开的东西 —— 一个按「白名单里有没有」分流的解析器会让同一句话在两台
    机器上走不同的路。

    长度上限分中英两套：中文应用名 6 个字够用（「网易云音乐」5、「酷狗音乐」4），而英文
    带空格的名字（``Visual Studio Code``）需要 20 个字符。这个差别不是凑数 —— 6 个汉字
    的上限正是把「我昨天说的那个想法」这类短语挡在外面的东西，而按字符数一刀切要么放它
    进来，要么把英文名字挡在外面。

    判错的代价是工具那边回一句「不在可启动的应用里，现在能开的是…」—— 一个有信息的失败，
    比沉默地落到 agent 好。
    """
    value = text.strip()
    if not value or any(char in value for char in '/\\:?*"<>|'):
        return False
    if any(word in value for word in _NOT_APP_WORDS):
        return False
    if _looks_like_path(value):
        return False
    han = sum(1 for char in value if "一" <= char <= "鿿")
    if han:
        return han <= 6 and len(value) <= 12
    return len(value) <= 20


#: 没有扩展名但确实是文件的那几个。全大写或首字母大写的 ASCII 名字在仓库里就是这一类，
#: 一一列出而不是按大小写猜：``README`` 是文件，``OK`` 不是。
_EXTENSIONLESS = frozenset(
    {"readme", "license", "licence", "makefile", "dockerfile", "changelog", "authors", "notice"}
)


def _looks_like_path(text: str) -> bool:
    """这串东西像不像一个路径。

    判据是**形状**而不是「文件在不在」：意图层不该碰文件系统 —— 一个按存在性分流的解析器
    会让同一句话在不同机器上走不同的路，而那种不确定性比偶尔多走一次 agent 糟得多。
    """
    value = text.strip()
    if not value:
        return False
    if "/" in value or "\\" in value:
        return True
    stem, dot, ext = value.rpartition(".")
    # 扩展名要像扩展名：``.md``、``.py``、``.toml``。「今天.我」不算。
    if dot and stem and ext.isascii() and ext.isalnum() and 1 <= len(ext) <= 6:
        return True
    return value.casefold() in _EXTENSIONLESS


#: 路径后面跟中文修饰语时，从哪里切。
#:
#: 「读一下 README.md 的第一行」整段捕获是 ``README.md 的第一行``，那不是任何文件的名字,
#: 于是快路径以 ``no such file`` 收场 —— 而说这句话的人只是想读那个文件。这是最自然的
#: 中文说法之一，把它留给 agent 也行，但 agent 要起一个进程读一个本机文件。
#:
#: **只在切之前那半已经像个文件名时才切**：含扩展点或路径分隔符。中文文件名（``报告.txt``）
#: 的第一个汉字出现在扩展点之前，切前那半是空的，所以它不会被误伤 —— 这条件就是为它加的。
_HAN = re.compile(r"[一-鿿]")


def _trim_path(captured: str) -> str:
    hit = _HAN.search(captured)
    if hit is None:
        return captured
    head = captured[: hit.start()].strip()
    if head and ("." in head or "/" in head or "\\" in head):
        return head
    return captured


__all__ = [
    "CODE_CAPABILITY",
    "RuleBasedIntentResolver",
    "is_dismissal",
    "required_capabilities",
]
