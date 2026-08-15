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
_POLITE = (
    r"(?:(?:请|請|麻烦|麻煩|帮我|幫我|帮忙|幫忙|能不能|可以|"
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
#: 「跑得动吗」matched ``跑`` and would have run「得动吗」as a shell command.
_QUESTION_TAIL = re.compile(r"(?:吗|嗎|呢|吧)\s*[?？]?\s*\Z")


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
            "web.search": _WEB_SEARCH_PATTERNS,
            "fs.read": _FS_READ_PATTERNS,
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
        for tool in ("web.search", "fs.read", "shell.run"):
            for pattern in self._compiled.get(tool, ()):
                match = pattern.search(stripped)
                if match is None:
                    continue
                arguments = self._extract(tool, match)
                if not self._plausible(arguments):
                    # A one-character capture is a quantifier artefact, not an
                    # argument. Keep looking, and fall through to the agent if
                    # nothing better matches.
                    continue
                return Intent(
                    kind="tool",
                    tool=tool,
                    arguments=arguments,
                    # Confidence is 1.0 for a hit, because the hit is the only
                    # evidence: there is no model score to average or threshold.
                    confidence=1.0,
                )
        return Intent(kind="agent", confidence=0.0)

    @staticmethod
    def _plausible(arguments: Mapping[str, Any]) -> bool:
        """Reject captures too short to be a real path, query, or command."""
        value = next(iter(arguments.values()), "")
        return isinstance(value, str) and len(value.strip()) >= MIN_ARGUMENT_LEN

    def _extract(self, tool: str, match: re.Match[str]) -> dict[str, Any]:
        """The named payload from the regex hit."""
        captured = match.group(1).strip() if match.lastindex else ""
        if tool == "fs.read":
            return {"path": captured}
        if tool == "web.search":
            return {"query": captured}
        if tool == "shell.run":
            return {"command": captured}
        return {}


__all__ = [
    "RuleBasedIntentResolver",
]
