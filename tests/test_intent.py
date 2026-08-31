"""Rule-based intent classification tests (ADR 005).

Two directions, and the second matters more. A hit routes straight to a local
tool and **executes it** -- so a false positive on `shell.run` runs a command the
user never asked for. A false negative only costs a few seconds, because the
agent still answers.

So the negative half of this file is not symmetry. It is the actual safety
property: every utterance below that merely *contains* a verb, rather than
*opening* with one, must fall through to ``kind="agent"``.

Historical failures pinned here, each with a case:

- unanchored ``search()`` turned 「你能看看这个问题吗」 into ``fs.read`` and
  「跑得动吗」 into ``shell.run``;
- a lazy ``(.+?)`` matched one character, so 「我想显示器换一个」 yielded
  ``path="器"`` -- wrong *and* unfalsifiable, since one character looks like a path;
- ``fs.read`` tested before ``web.search``, so 「读一下网页 X」 read a file.
"""

from __future__ import annotations

import pytest

from core.dispatch.intent import MIN_ARGUMENT_LEN, RuleBasedIntentResolver


@pytest.fixture
def resolver() -> RuleBasedIntentResolver:
    return RuleBasedIntentResolver()


# -- hits: fs.read ------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance, path",
    [
        ("读一下 config.toml", "config.toml"),
        ("读一下config.toml", "config.toml"),
        ("查看 README.md", "README.md"),
        ("显示 core/events.py", "core/events.py"),
        ("cat requirements-dev.txt", "requirements-dev.txt"),
        ("read the README file", "README"),
        ("show file docs/routines.md", "docs/routines.md"),
        ("read pyproject.toml", "pyproject.toml"),
    ],
)
def test_fs_read_hits(resolver, utterance, path):
    intent = resolver.resolve(utterance)
    assert intent.kind == "tool"
    assert intent.tool == "fs.read"
    assert intent.arguments == {"path": path}
    assert intent.confidence == 1.0


# -- hits: web.search ---------------------------------------------------------


@pytest.mark.parametrize(
    "utterance, query",
    [
        ("搜一下 武汉天气", "武汉天气"),
        ("请帮我搜一下 武汉天气", "武汉天气"),
        ("搜索 sherpa-onnx 版本", "sherpa-onnx 版本"),
        ("查找 FTS5 中文分词", "FTS5 中文分词"),
        ("网上找 tauri 透明窗口", "tauri 透明窗口"),
        ("search for rust async", "rust async"),
        ("look up circuit breaker pattern", "circuit breaker pattern"),
        ("google whisper streaming", "whisper streaming"),
    ],
)
def test_web_search_hits(resolver, utterance, query):
    intent = resolver.resolve(utterance)
    assert intent.kind == "tool"
    assert intent.tool == "web.search"
    assert intent.arguments == {"query": query}


def test_a_web_marker_beats_the_read_verb(resolver):
    """「读一下网页 X」 opens with 「读」 but means search.

    Order is the whole fix: the web marker is the more specific signal, so
    ``web.search`` is tested first.
    """
    intent = resolver.resolve("读一下网页 example.com")
    assert intent.tool == "web.search"
    assert intent.arguments == {"query": "example.com"}


# -- hits: shell.run ----------------------------------------------------------


@pytest.mark.parametrize(
    "utterance, command",
    [
        ("运行 pytest tests -q", "pytest tests -q"),
        ("运行一下 npm run build", "npm run build"),
        ("执行命令 cargo check", "cargo check"),
        ("跑 pytest", "pytest"),
        ("run cargo test", "cargo test"),
        ("execute the command git status", "git status"),
        ("git status", "git status"),
        ("npm run build", "npm run build"),
        ("cargo check", "cargo check"),
    ],
)
def test_shell_run_hits(resolver, utterance, command):
    intent = resolver.resolve(utterance)
    assert intent.kind == "tool"
    assert intent.tool == "shell.run"
    assert intent.arguments == {"command": command}


def test_a_hit_does_not_mean_the_command_will_run(resolver):
    """The resolver classifies; ``core.tools.policy`` decides.

    ``shell.run`` is off by default and non-whitelisted commands are refused
    rather than queried -- so a hit here is the *start* of the gate, not a
    bypass of it. This test exists to keep that division stated in the tests as
    well as the docstrings.
    """
    intent = resolver.resolve("运行 rm -rf /")
    assert intent.tool == "shell.run"
    assert intent.arguments == {"command": "rm -rf /"}


# -- misses: the safety half --------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        # A verb inside a question, not at the front.
        "你能看看这个问题吗",
        "帮我看看日程",
        "我想显示器换一个",
        "跑得动吗",
        "这个能运行吗",
        "我读过那本书",
        "I read that git is hard",
        "运行时报错了怎么办",
        # Conversation about the tools rather than a request to use them.
        "搜索引擎是怎么工作的",
        "文件读取速度慢",
        # A question particle turns a command shape back into a question.
        "要不要执行呢",
        "现在跑吧",
        # Ambiguous verbs get no politeness allowance.
        "请帮我查看这个问题",
        "麻烦显示一下你的想法",
    ],
)
def test_these_must_not_become_tool_calls(resolver, utterance):
    intent = resolver.resolve(utterance)
    assert intent.kind == "agent", f"{utterance!r} became {intent.tool}"
    assert intent.tool is None
    assert intent.confidence == 0.0


def test_a_question_tail_blocks_an_otherwise_valid_command(resolver):
    """「跑得动吗」 matched ``跑`` and would have run 「得动吗」."""
    assert resolver.resolve("运行 pytest").kind == "tool"
    assert resolver.resolve("运行 pytest 吗").kind == "agent"
    assert resolver.resolve("运行 pytest 吗？").kind == "agent"


def test_a_one_character_capture_is_rejected_as_a_quantifier_artefact(resolver):
    """One character is never a path, a query, or a command."""
    assert MIN_ARGUMENT_LEN == 2
    assert resolver.resolve("读一下 a").kind == "agent"
    assert resolver.resolve("查看 x").kind == "agent"


def test_empty_and_whitespace_fall_through(resolver):
    for utterance in ("", "   ", "\n\t"):
        assert resolver.resolve(utterance).kind == "agent"


def test_a_bare_verb_with_no_argument_falls_through(resolver):
    for utterance in ("运行", "搜一下", "读一下", "cat"):
        assert resolver.resolve(utterance).kind == "agent"


# -- politeness ---------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "请搜一下 武汉天气",
        "帮我搜一下 武汉天气",
        "请帮我搜一下 武汉天气",
        "麻烦帮忙搜一下 武汉天气",
        "can you search for 武汉天气",
    ],
)
def test_stacked_politeness_still_matches_an_explicit_verb(resolver, utterance):
    """「请帮我搜一下」 stacks two openers, so one allowance was not enough."""
    assert resolver.resolve(utterance).tool == "web.search"


def test_politeness_is_not_granted_to_the_ambiguous_verbs(resolver):
    """Granting it to 「查看」 would turn 「请帮我查看这个问题」 into a file read.

    That is the trade this file is built around: 「查看 README.md」 still works
    bare, and the polite form loses to safety.
    """
    assert resolver.resolve("查看 README.md").tool == "fs.read"
    assert resolver.resolve("请帮我查看这个问题").kind == "agent"


# -- wiring -------------------------------------------------------------------


def test_custom_patterns_replace_the_shipped_set():
    resolver = RuleBasedIntentResolver(
        patterns={"fs.read": (r"\Aopen sesame (.+)",)}
    )
    assert resolver.resolve("open sesame vault.txt").arguments == {"path": "vault.txt"}
    # The shipped patterns are gone, not merged.
    assert resolver.resolve("搜一下 武汉天气").kind == "agent"


def test_patterns_are_compiled_once_at_construction():
    resolver = RuleBasedIntentResolver()
    for tool, raw in resolver.patterns.items():
        assert len(resolver._compiled[tool]) == len(raw)


def test_resolution_is_deterministic(resolver):
    first = resolver.resolve("搜一下 武汉天气")
    second = resolver.resolve("搜一下 武汉天气")
    assert first == second


# -- 简单的事平台自己做：时间、应用、网页 ---------------------------------------


@pytest.mark.parametrize(
    "text",
    ["现在几点了", "几点了", "现在时间", "现在是几点", "今天星期几", "今天几号",
     "报时", "what time is it"],
)
def test_asking_the_time_never_reaches_an_agent(resolver, text):
    """答案在本机时钟里。派出去要几秒和一次出网，而快路径实测 15 ms。"""
    intent = resolver.resolve(text)

    assert intent.tool == "time.now"
    # 时区是本机的属性，不是这句话的属性 —— 所以没有参数可提。
    assert intent.arguments == {}


@pytest.mark.parametrize("text", ["放点音乐", "来首歌", "听音乐", "放歌", "play some music"])
def test_a_generic_music_request_opens_the_default_player(resolver, text):
    """泛指的「放点音乐」没有曲目，所以是「打开那个播放器」而不是「搜这首歌」。
    开哪个由 ``apps.default_music`` 定，不在这一层。"""
    intent = resolver.resolve(text)

    assert intent.tool == "app.open"
    assert intent.arguments == {}


@pytest.mark.parametrize(
    "text, name",
    [("打开网易云", "网易云"), ("打开网易云音乐", "网易云音乐"), ("启动酷狗", "酷狗"),
     ("开启微信", "微信"), ("open Visual Studio Code", "Visual Studio Code")],
)
def test_opening_a_short_name_is_an_app(resolver, text, name):
    assert resolver.resolve(text).arguments == {"name": name}


@pytest.mark.parametrize(
    "text",
    ["打开一下我昨天说的那个想法", "打开这个问题看看", "打开我的想法", "打开那些文件看看"],
)
def test_a_sentence_is_not_an_app_name(resolver, text):
    """「打开」对文件、网址和应用三样都歧义，而形状是唯一能在不碰文件系统、不读工具配置
    的前提下分开它们的东西。

    两道线：中文 6 个字的上限挡住长短语，指示代词和动词性尾巴（``_NOT_APP_WORDS``）挡住
    短的那些 —— 「打开这个问题看看」只有 6 个汉字，光靠长度过不了。
    """
    assert resolver.resolve(text).kind == "agent"


@pytest.mark.parametrize(
    "text, url",
    [
        ("打开 https://www.bilibili.com", "https://www.bilibili.com"),
        ("访问 http://example.com/x", "http://example.com/x"),
        # 说主机名的人省略了协议，补上是补一个他省的字。https 而不是 http：
        # 降级到明文得是个显式选择。
        ("上 www.bilibili.com", "https://www.bilibili.com"),
    ],
)
def test_an_address_goes_to_the_browser(resolver, text, url):
    intent = resolver.resolve(text)

    assert intent.tool == "web.open"
    assert intent.arguments == {"url": url}


@pytest.mark.parametrize(
    "text, query",
    [("播放周杰伦的稻香", "周杰伦的稻香"), ("放一首稻香", "一首稻香"), ("听周杰伦", "周杰伦")],
)
def test_playing_something_specific_opens_a_search_page(resolver, text, query):
    """这句话要的是一个能点播放的页面 —— 渲染它的是浏览器，不是我们。所以是 ``web.open``
    而不是 ``web.search``（后者把结果抓回来给平台读）。"""
    intent = resolver.resolve(text)

    assert intent.tool == "web.open"
    assert intent.arguments == {"query": query}


def test_searching_still_goes_to_web_search_not_web_open(resolver):
    """两条路的分别是「结果给谁看」：``web.search`` 给平台，``web.open`` 给浏览器。"""
    assert resolver.resolve("搜一下 幂等是什么").tool == "web.search"


@pytest.mark.parametrize(
    "text",
    ["你好", "帮我写一个 python 脚本", "检查目前运行状态是否正常", "这个项目还差什么"],
)
def test_conversation_and_real_work_still_go_to_an_agent(resolver, text):
    """新增的四组规则一条都不该把这些抢过去 —— 它们是派发存在的理由。"""
    assert resolver.resolve(text).kind == "agent"


# -- 路径后面跟中文修饰语 -------------------------------------------------------


def test_a_chinese_qualifier_after_the_path_is_trimmed(resolver):
    """「读一下 X 的第一行」是最自然的说法之一，而整段捕获不是任何文件的名字。

    修之前这一句以 ``no such file`` 收场（实测），而说话的人只是想读那个文件。
    """
    assert resolver.resolve("读一下 README.md 的第一行").arguments == {"path": "README.md"}
    assert resolver.resolve("读一下 docs/routines.md 的开头").arguments == {
        "path": "docs/routines.md"
    }


def test_a_chinese_filename_is_not_trimmed_away(resolver):
    """切的前提是「切之前那半已经像个文件名」。中文文件名的第一个汉字在扩展点之前，
    所以切前那半是空的 —— 这个条件就是为它加的。"""
    assert resolver.resolve("读一下 报告.txt").arguments == {"path": "报告.txt"}
    assert resolver.resolve("读 中文目录/说明.md").arguments == {"path": "中文目录/说明.md"}


def test_a_path_with_spaces_still_survives(resolver):
    """按空白切会破坏这一类，所以切的依据是汉字而不是空白。"""
    assert resolver.resolve("读 my file.txt").arguments == {"path": "my file.txt"}


def test_a_search_query_is_never_trimmed(resolver):
    """查询词几乎总是中文，按汉字切会把每一个查询都截成空 —— 只有 fs.read 走这条。"""
    assert resolver.resolve("搜索 幂等 是什么意思").arguments == {"query": "幂等 是什么意思"}


# ------------------------------------------------- 能力标注（2026-09-01 的路由修正）


@pytest.mark.parametrize(
    "text",
    [
        "帮我写个 Python 脚本统计目录大小",
        "改一下这个函数的返回值",
        "这个项目里的测试为什么跑不起来",
        "跑一下测试",
        "git status 看看",
        "提交一下",
        "报错了，帮我看看",
        "重构这段代码",
        "write a script that renames files",
        "refactor this function's error handling",
        "debug the failing test",
    ],
)
def test_a_request_that_needs_the_machine_asks_for_the_code_capability(text):
    """这些说法要一个**真能动这台机器**的后端。

    漏判的代价是具体的：一句「跑一下测试」落到裸 HTTP 端点上，换回来的是一段它其实
    执行不了的说明 —— 语法正确、事实全错，而回合报成功。
    """
    from core.dispatch.intent import CODE_CAPABILITY, required_capabilities

    assert required_capabilities(text) == frozenset({CODE_CAPABILITY})


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "今天天气怎么样",
        "帮我写一封邮件给张三",
        "什么是幂等",
        "给我讲个笑话",
        "我有点累了",
        "翻译一下这句话",
        "how are you",
        "",
        "   ",
    ],
)
def test_ordinary_conversation_asks_for_nothing(text):
    """空集 = 谁都行，于是最便宜最快的后端赢。

    **反方向的误判在这里是有代价的**：把闲聊标成「要动机器」会让每一句话去起一个
    CLI 进程（自报 2500ms，实测更慢），而语音里 0 秒和 3 秒是「即时」与「迟钝」的差别。
    「写一封邮件」是这条线上最容易被误伤的那个 —— 它有「写」，物件不是代码。
    """
    from core.dispatch.intent import required_capabilities

    assert required_capabilities(text) == frozenset()


def test_the_resolver_carries_the_same_answer(resolver):
    """挂在解析器上是为了让「换掉解析器」把两件事一起换掉。"""
    from core.dispatch.intent import required_capabilities

    for text in ("写个脚本", "你好"):
        assert resolver.capabilities(text) == required_capabilities(text)
