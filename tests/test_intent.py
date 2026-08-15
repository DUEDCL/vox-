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
