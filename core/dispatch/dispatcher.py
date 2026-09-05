"""The dispatcher (ADR 005).

Resolve intent, then either run a local tool or route to agents. The tool path
is the fast one: a rule hit executes in milliseconds and no agent is involved.
The agent path plans through the router, streams through the aggregator, and
feeds every outcome back so the next route is better informed.

What lives here rather than in the router or aggregator:

- **The tool-vs-agent fork**, because it is the one decision that needs both
  the intent resolver and the tool runner.
- **``cancel()`` on every dispatched adapter** once the merged stream ends. The
  aggregator can stop *reading* a loser but cannot reach its adapter; only the
  dispatcher holds both. A ``race`` that leaves the losing subprocess running
  is the leak P5's ``finally`` was written to prevent, and this is the other
  half of it.
- **Outcome recording**, because ok/failed is only known once the terminating
  chunk has been seen.

The dispatcher does not open adapters. Opening them reads configuration and
touches the filesystem (``core.agents.registry``); the caller passes the
already-open mapping, so enabling or disabling an agent takes effect without
rebuilding this object.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping

from core.agents.contract import AgentChunk, Task
from core.agents.skills import MAX_CALLS, parse_calls, render_result, strip_calls
from core.dispatch.contract import (
    Aggregator,
    DispatchPlan,
    Intent,
    IntentResolver,
    Router,
)
from core.events import AGENT_SCHEMA_PATH, build_event, validate_event
from core.tools.contract import ToolRequest


#: 一轮对话里最多回去问模型几次。**这不是 `MAX_CALLS` 的复读**：那个数管的是「执行了几个
#: 工具」，这个管的是「多花了几次 LLM 往返」，而后者才是使用者等待的来源。
#:
#: 同一轮里的多个调用一次执行完，所以「查时间 + 开应用」这类独立请求只用掉一次往返，
#: 而**依赖**前一步结果的第二步会占掉第二次。2 是有意留窄的：语音的耐心以秒计，
#: 而每一次往返在百炼 flash 档是 1.5–2.4 s（2026-09-05 实测）。
MAX_TOOL_ROUNDS = 2


_SAFE_FAILURE_REASONS = frozenset(
    {
        "stream ended without a terminal chunk",
        "no agents available",
    }
)
_SAFE_FAILURE_PATTERNS = (
    re.compile(r"^exit -?\d+$"),
    re.compile(r"^timed out after \d+(?:\.\d+)?s$"),
    re.compile(r"^output exceeded \d+ characters$"),
)


def _public_failure_reason(error: str | None) -> str:
    """Return a safe task-event summary, never an agent-provided message.

    Agent stderr and protocol error fields can contain the prompt, a reply, or
    credentials echoed by a backend. They remain available on the internal
    ``AgentChunk`` for local handling, but the event stream fans out to
    logs/transports and may only carry fixed, shape-validated summaries.
    """
    if not isinstance(error, str):
        return "agent reported failure"
    candidate = error.strip()
    if candidate in _SAFE_FAILURE_REASONS:
        return candidate
    if any(pattern.fullmatch(candidate) for pattern in _SAFE_FAILURE_PATTERNS):
        return candidate
    return "agent reported failure"


@dataclass(frozen=True)
class DispatchResult:
    """One dispatched turn: what ran, what came back, how long it took.

    ``chunks`` is materialised rather than lazy. The caller needs the outcome
    (ok or failed) to decide what to speak, and that is only known once the
    terminating chunk has arrived -- a lazy result would report success before
    the failure it is about to yield. ``stream()`` is there for callers that
    want increments as they arrive.
    """

    #: ``tool`` when a rule hit ran a local tool, ``agent`` when it was routed,
    #: ``none`` when neither could run.
    route: str
    chunks: tuple[AgentChunk, ...] = ()
    #: Agents actually dispatched to -- the plan's agents, not the winner. The
    #: winner is whoever produced the text chunks.
    agents: tuple[str, ...] = ()
    tool: str | None = None
    elapsed_ms: int = 0
    reason: str = ""
    ok: bool = False
    #: The policy allows this in principle but wants an explicit user action
    #: first. Never auto-confirmed, never spoken as a failure (FR-6.13).
    needs_confirmation: bool = False

    @property
    def text(self) -> str:
        """Every text chunk concatenated -- what the TTS layer would speak."""
        return "".join(chunk.text for chunk in self.chunks if chunk.kind == "text")


def _as_mapping(value: Any) -> dict[str, Any]:
    """安全地把一个可能根本不是 Mapping 的东西变成 dict。

    日志的参数在**调用 ``_detail`` 之前**就求值了，所以这一步不能抛 —— ``_detail`` 内部的
    try 保护不到它。而调用方给的 ``outcome`` 不一定是真的 ``ToolResult``（测试里是 Mock，
    ``dict(Mock().audit)`` 会炸）。日志绝不能改变一轮的结果，包括不能因为构造它的参数。
    """
    if isinstance(value, Mapping):
        return dict(value)
    return {}


class Dispatcher:
    """Intent → tool or agents → merged stream.

    ``router``, ``aggregator``, and ``resolver`` are injected. ``tool_runner``
    is optional and opt-in, matching the memory and tools wiring in
    ``vox_plugin.plugin``: without it, a tool-shaped utterance falls through
    to an agent rather than failing, because an agent can still answer
    「读一下 config.toml」by other means.
    """

    def __init__(
        self,
        router: Router,
        aggregator: Aggregator,
        *,
        resolver: IntentResolver | None = None,
        tool_runner: Any = None,
        memory_recaller: Any = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_detail: Callable[..., None] | None = None,
    ) -> None:
        self.router = router
        self.aggregator = aggregator
        self.resolver = resolver
        self.tool_runner = tool_runner
        self.memory_recaller = memory_recaller
        self._on_event = on_event
        #: 「给人看的运行细节」出口，签名 ``(source, message, level=..., **fields)``。
        #:
        #: 和 ``on_event`` 分工在**扇出面**上，不在详细程度上：事件会到球、到传输、到每个
        #: 外部消费者，所以它不带文本和参数；这条只到本机控制台的日志视图，所以它带 ——
        #: 而「``fs.read`` 收到的 path 到底是什么」只有带参数才答得出。
        self._on_detail = on_detail
        #: Event delivery is a side channel and must not change a turn result.
        self.sink_failures = 0

    def _detail(self, source: str, message: str, **fields: Any) -> None:
        """写一条运行细节。吞掉一切异常 —— 日志失败不能改变一轮的结果。"""
        if self._on_detail is None:
            return
        try:
            self._on_detail(source, message, **fields)
        except Exception:  # noqa: BLE001 - a log sink is never load-bearing
            self.sink_failures += 1

    # -- public --------------------------------------------------------------

    def dispatch(
        self,
        task: Task,
        adapters: Mapping[str, Any] | None = None,
        *,
        speaker: str | None = None,
    ) -> DispatchResult:
        """Run one turn to completion. The blocking, materialised path."""
        started = time.monotonic()
        intent = self.resolve(task.text)
        self._detail(
            "intent",
            f"{task.text[:80]} -> {intent.kind}" + (f" / {intent.tool}" if intent.tool else ""),
            kind=intent.kind,
            tool=intent.tool or "",
            arguments=dict(intent.arguments or {}),
            confidence=intent.confidence,
            tool_runner=self.tool_runner is not None,
        )
        if intent.kind == "tool" and self.tool_runner is not None:
            return self._run_tool(task, intent, started, speaker)
        return self._run_agents(
            self._require_capabilities(self._recall_context(task)), adapters or {}, started
        )

    def stream(
        self,
        task: Task,
        adapters: Mapping[str, Any] | None = None,
        *,
        speaker: str | None = None,
    ) -> Iterator[AgentChunk]:
        """Increments as they arrive, for the voice layer's first-token path.

        The outcome bookkeeping still happens -- in the generator's ``finally``,
        so abandoning the stream records the turn and cancels the adapters just
        as completing it would.
        """
        started = time.monotonic()
        intent = self.resolve(task.text)
        if intent.kind == "tool" and self.tool_runner is not None:
            result = self._run_tool(task, intent, started, speaker)
            yield from result.chunks
            return
        task = self._recall_context(task)
        task = self._require_capabilities(task)
        plan = self.router.plan(task)
        available = self._collect(plan, adapters or {})
        if not available:
            yield AgentChunk(
                kind="done",
                error=plan.reason or "no agents available",
                elapsed_ms=self._ms_since(started),
            )
            return
        yield from self._stream_agents(task, plan, available, started)

    def resolve(self, text: str) -> Intent:
        """Classify an utterance. No resolver attached means always ``agent``."""
        if self.resolver is None:
            return Intent(kind="agent")
        return self.resolver.resolve(text)

    def _recall_context(self, task: Task) -> Task:
        """Augment the task with recalled memory, before agent routing only.

        Facts (mid-layer) and recent turns (short-layer) become plain-text
        context lines, rendered ahead of the question by ``render_prompt``. A
        tool intent never reaches this method: a local tool has no prompt to
        augment, and the fast path should stay fast.

        Memory is an enhancement, never a precondition, and each source is
        guarded separately -- a locked database or a recaller that lacks one
        method must not take the turn down, and one failing source must not
        hide the other's results.
        """
        recaller = self.memory_recaller
        if recaller is None:
            return task
        items: list[str] = []
        try:
            for record in recaller.facts(task.text):
                if record.text.strip():
                    items.append(record.text)
        except Exception:  # noqa: BLE001 - memory is an enhancement
            pass
        try:
            for record in recaller.recent_turns(session_id=task.session_id):
                if record.text.strip():
                    items.append(record.text)
        except Exception:  # noqa: BLE001 - memory is an enhancement
            pass
        if not items:
            return task
        # Deduplicate while preserving order: a fact may also be a recent turn.
        return replace(task, context=task.context + tuple(dict.fromkeys(items)))

    def run_intent(
        self, task: Task, intent: Intent, *, speaker: str | None = None
    ) -> DispatchResult:
        """Run an intent the caller supplies instead of one resolved from text.

        This is the other half of ``needs_confirmation``. ``_run_tool`` says the
        caller re-submits with ``confirmed = True``; without a door, the caller's
        only options were to reach into a private method or to rebuild the
        outcome mapping somewhere else and let the two drift.

        The invariant is unchanged and is worth restating: **nothing in this
        class ever puts ``confirmed`` into an intent.** It arrives here because
        a user clicked, or it does not arrive.
        """
        started = time.monotonic()
        if intent.kind == "tool" and self.tool_runner is not None:
            return self._run_tool(task, intent, started, speaker)
        return DispatchResult(
            route="none",
            elapsed_ms=self._ms_since(started),
            reason="no tool runner for a tool intent"
            if intent.kind == "tool"
            else f"run_intent only runs tools, got {intent.kind!r}",
        )

    # -- tool path -----------------------------------------------------------

    def _run_tool(
        self,
        task: Task,
        intent: Intent,
        started: float,
        speaker: str | None,
    ) -> DispatchResult:
        """Execute one local tool. The gate is the runner's, not ours.

        No policy decision is duplicated here. ``core.tools.policy`` is the one
        gate for both origins (FR-9.8), so a second check at this layer could
        only ever disagree with it -- and a gate that can disagree with itself
        is not a gate.

        ``speaker`` is the name the wake-time voiceprint gate verified, passed
        in by the caller rather than invented here. P4 left this deliberately
        unwired: the plugin does not fabricate a verified speaker, so
        ``shell.run`` was refused through that path by construction. Carrying
        the real name is this stage's job, and ``None`` still means refused --
        the dispatcher can only forward a verification, never assert one.
        """
        assert intent.tool is not None  # kind == "tool" implies a tool name
        request = ToolRequest(
            tool=intent.tool,
            arguments=dict(intent.arguments),
            origin="voice",
            speaker=speaker,
        )
        try:
            outcome = self.tool_runner.run(request)
        except Exception as exc:  # noqa: BLE001 - reported as a failed turn
            return DispatchResult(
                route="tool",
                chunks=(
                    AgentChunk(
                        kind="done",
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=self._ms_since(started),
                    ),
                ),
                tool=intent.tool,
                elapsed_ms=self._ms_since(started),
                reason=f"{type(exc).__name__}: {exc}",
                ok=False,
            )
        chunks: list[AgentChunk] = []
        if outcome.output:
            chunks.append(AgentChunk(kind="text", text=outcome.output))
        chunks.append(
            AgentChunk(
                kind="done",
                elapsed_ms=self._ms_since(started),
                error=None if outcome.ok else (outcome.error or "tool failed"),
            )
        )
        # 工具跑完了，把参数和结果记进运行日志 —— 「route=tool ok=false 0ms」这种报告缺的
        # 就是这两样：哪个 path、被谁拒的。事件契约不带参数（它扇出到每个通道），所以这条
        # 走日志。
        self._detail(
            "tool",
            f"{intent.tool} {'ok' if outcome.ok else '失败：' + (outcome.error or 'tool failed')}",
            level="info" if outcome.ok else "error",
            tool=intent.tool,
            arguments=dict(intent.arguments or {}),
            ok=outcome.ok,
            error=outcome.error or "",
            needs_confirmation=outcome.needs_confirmation,
            elapsed_ms=self._ms_since(started),
            output_chars=len(outcome.output or ""),
            audit=_as_mapping(getattr(outcome, "audit", None)),
        )
        return DispatchResult(
            route="tool",
            chunks=tuple(chunks),
            tool=intent.tool,
            elapsed_ms=self._ms_since(started),
            reason=outcome.error or "",
            ok=outcome.ok,
            # A confirmation request is not a failure and must not be spoken as
            # one: the orb has to show the command and wait (FR-6.13). The
            # caller re-submits with ``confirmed = True``; nothing here retries
            # on its own.
            needs_confirmation=outcome.needs_confirmation,
        )

    # -- agent path ----------------------------------------------------------

    def _run_agents(
        self, task: Task, adapters: Mapping[str, Any], started: float
    ) -> DispatchResult:
        plan = self.router.plan(task)
        available = self._collect(plan, adapters)
        if not available and task.capabilities:
            # **能力闸门降级。** 闸门把这一句判给了唯一能做的后端，而那个后端这台机器上
            # 没装（实测：`claude` 在 `%APPDATA%\npm`，那个目录不在 PATH 上）。此前的行为
            # 是整轮失败，使用者听到的是「agent 报告失败」—— 一句既不回答问题也不说明
            # 原因的话。
            #
            # 退一步用**能力不设限**的计划：那个后端答不了「帮我改这个函数」，但它的
            # system prompt 明确要求它说「这个我做不到」（core/agents/environment.py），
            # 而那是一个诚实且有用的回答。理由写进 reason，日志里能查到是降级来的。
            relaxed = replace(task, capabilities=frozenset())
            fallback = self.router.plan(relaxed)
            available = self._collect(fallback, adapters)
            if available:
                self._detail(
                    "agent",
                    f"能力降级：{plan.reason} → 改用 {', '.join(fallback.agents)}",
                    level="warn",
                    wanted=sorted(task.capabilities),
                    blocked_reason=plan.reason,
                    agents=list(fallback.agents),
                )
                task, plan = relaxed, replace(
                    fallback, reason=f"{fallback.reason}（降级：{plan.reason}）"
                )
        if not available:
            reason = plan.reason or "no agents available"
            return DispatchResult(
                route="none",
                agents=(),
                elapsed_ms=self._ms_since(started),
                reason=reason,
                ok=False,
            )
        collected = tuple(self._stream_agents(task, plan, available, started))
        terminal = collected[-1] if collected else None
        ok = terminal is not None and terminal.kind == "done" and not terminal.error
        # agent 要求跑一个工具？跑掉，把结果回给它，再收一次回答。
        if ok:
            collected, ok = self._settle_tool_calls(task, plan, available, started, collected, ok)
            terminal = collected[-1] if collected else terminal
        backends = self._backends(available)
        self._detail(
            "agent",
            f"{plan.mode} → {', '.join(backends)}"
            + ("" if ok else f"（失败：{terminal.error if terminal else 'no chunks'}）"),
            level="info" if ok else "error",
            mode=plan.mode,
            agents=[name for name, _ in available],
            # 「谁答的」要能查。见 ``_backends``。
            backends=backends,
            # 为什么是它答的：闸门要求了什么、评分是多少。一个不带这两样的路由记录
            # 回答不了「为什么不是另一个」。
            capabilities=sorted(task.capabilities),
            reason=plan.reason,
            ok=ok,
            error=(terminal.error if terminal else "") or "",
            elapsed_ms=self._ms_since(started),
            # 有多少个 chunk 到了：一个「成功但零 chunk」的回合看起来和正常的一样。
            chunks=len(collected),
            context_lines=len(task.context or ()),
        )
        return DispatchResult(
            route="agent",
            chunks=collected,
            agents=tuple(name for name, _ in available),
            elapsed_ms=self._ms_since(started),
            reason=plan.reason,
            ok=ok,
        )

    def _execute_tool(self, name: str, arguments: Mapping[str, Any]) -> tuple[bool, str]:
        """跑一个 agent 点名的工具。返回 (成功, 给模型看的详情)。

        **来源是 agent，说话人不传。** 一个由模型发起的请求不该继承使用者的声纹身份 ——
        那正是 `shell.run` 的 `require_verified_speaker` 要挡住的东西。执行走的是和语音
        快路径同一套闸门，所以这里只决定「谁在发起」，不决定「什么能被执行」。
        """
        request = ToolRequest(tool=name, arguments=dict(arguments), origin="agent")
        try:
            outcome = self.tool_runner.run(request)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - 工具炸了也要把话说完
            tool_ok, detail = False, f"{type(exc).__name__}: {exc}"
        else:
            tool_ok = bool(outcome.ok)
            detail = (outcome.output or outcome.error or "").strip()
        self._detail(
            "agent",
            f"agent 要求 {name} → {'成功' if tool_ok else '失败'}",
            level="info" if tool_ok else "warn",
            tool=name,
            arguments=dict(arguments),
            ok=tool_ok,
            detail=detail[:200],
        )
        return tool_ok, detail

    def _settle_tool_calls(
        self,
        task: Task,
        plan: Any,
        available: tuple[tuple[str, Any], ...],
        started: float,
        collected: tuple[AgentChunk, ...],
        ok: bool,
    ) -> tuple[tuple[AgentChunk, ...], bool]:
        """agent 说要跑一个工具 —— 跑掉，把结果回给它，再收一次回答。

        ## 为什么需要这一整段

        使用者实测：他说「帮我打开网易云音乐」（ASR 听成「试了给我打开…」），正则没命中，
        落到 agent，agent 回 **「好，正在打开网易云音乐。」然后什么都没发生。** 那不是模型在
        撒谎 —— 请求能到它那里，而能力不在它手上，说得像做过了是它最容易的出路。这一段把
        能力交给它。

        ## 三条边界

        1. **只解析，不放行。** 执行走 `self.tool_runner`，也就是和语音快路径**同一套闸门**
           （沙箱、白名单、危险模式、`shell.run` 的确认与声纹）。所以这个功能扩大的是
           「谁能发起」，不是「什么能被执行」—— 而 `shell.run` 根本不在 agent 看得到的清单里。
        2. **多步，但有界**（`MAX_CALLS` 次执行 / `MAX_TOOL_ROUNDS` 次再问）。**2026-09-05
           从「一轮一次」改过来** —— 原来那个 1 的理由是延迟预算，而预算在换掉 LLM 端点之后
           变了（中转站 `claude-opus-5` 首字 7.9 s 且不增量下发；百炼 flash 档 1.5–2.4 s）。
           「先查时间再打开网易云」这类请求在一次调用的上限下必然只做一半，而使用者听到的是
           一句把两件事都汇报了的话 —— 说得像做过了，正是这一整段要修的毛病。
        3. **同一轮里的多个调用一次执行完再问。** 独立的两件事因此只多花一次往返；而**依赖**
           前一步结果的第二步模型自己会分轮说出来（它第一轮只写得出第一个）。反过来做
           （每步都回去问一次）会让独立的两件事也各花一次往返。
        4. **失败要回给它。** 拿不到「失败」这一位，它会把「拒绝了」当成「做完了」然后向用户
           汇报成功 —— 那正是这一段要修的毛病，不能在这里重新引入。

        没有工具调用（绝大多数轮）时原样返回，一次多余的对象构造都不做。
        """
        if self.tool_runner is None:
            return collected, ok
        current, current_ok = collected, ok
        budget = MAX_CALLS
        last_detail = ""
        last_tool_ok = False
        did_run = False
        for step in range(MAX_TOOL_ROUNDS):
            text = "".join(chunk.text or "" for chunk in current if chunk.kind == "text")
            calls = parse_calls(text)[:budget]
            if not calls:
                break
            results: list[str] = []
            for name, arguments in calls:
                last_tool_ok, last_detail = self._execute_tool(name, arguments)
                results.append(render_result(name, last_tool_ok, last_detail))
                did_run = True
            budget -= len(calls)
            asked = "、".join(name for name, _ in calls)
            followup = replace(
                task,
                id=f"{task.id}-tool{step + 1}" if step else f"{task.id}-tool",
                context=tuple(task.context or ())
                + (f"你刚才要求 {asked}，结果如下。用一句话把结果告诉用户。",)
                + tuple(results),
            )
            current = tuple(self._stream_agents(followup, plan, available, started))
            last = current[-1] if current else None
            current_ok = last is not None and last.kind == "done" and not last.error
            if not current_ok:
                # 再问那一轮失败时**不要丢掉工具已经做了这件事**：拿工具的结果直接当回答，
                # 比一句「agent 失败了」有用得多 —— 应用其实已经开起来了。
                spoken = last_detail or ("做好了" if last_tool_ok else "没做成")
                return (
                    AgentChunk(kind="text", text=spoken[:200]),
                    AgentChunk(kind="done"),
                ), last_tool_ok
            if budget <= 0:
                break
        if not did_run:
            return collected, ok
        # 把调用标记从最终文本里去掉：模型有时会在汇报里把它重复一遍，而**用完预算之后**
        # 那一行不会再被执行 —— 留着它就是让使用者听见一串符号。
        cleaned = tuple(
            replace(chunk, text=strip_calls(chunk.text or "")) if chunk.kind == "text" else chunk
            for chunk in current
        )
        return cleaned, current_ok

    def _collect(
        self, plan: DispatchPlan, adapters: Mapping[str, Any]
    ) -> tuple[tuple[str, Any], ...]:
        """Pair planned names with live adapters, dropping names with none.

        A planned agent missing from ``adapters`` is a configuration mismatch,
        not an agent failure, so it is **not** recorded against its success rate
        or its breaker. Punishing an agent for the caller forgetting to open it
        would eventually route around a perfectly healthy backend.

        It **is** released. ``plan()`` took out a load slot for every name it
        returned, and a name dropped here is never dispatched, so nothing else
        will ever give that slot back -- the agent's load score would decay
        monotonically until it stopped being routed to at all. Dropping and
        releasing happen in the same place so they cannot drift apart.
        """
        paired: list[tuple[str, Any]] = []
        for name in plan.agents:
            adapter = adapters.get(name)
            if adapter is None:
                self._release(name)
                continue
            paired.append((name, adapter))
        return tuple(paired)

    def _release(self, name: str) -> None:
        """Give back a load slot. Optional on the router protocol, so guarded."""
        releaser = getattr(self.router, "release", None)
        if not callable(releaser):
            return
        try:
            releaser(name)
        except Exception:  # noqa: BLE001 - bookkeeping must not fail a turn
            pass

    def _require_capabilities(self, task: Task) -> Task:
        """把「这句话要什么后端」填进 task，让路由的能力闸门真的生效。

        **这一步此前不存在，而它的缺席让能力声明成了装饰。** 5 维评分里 cost 与 latency
        占 70%，所以一个裸 HTTP 端点必然赢过要起进程的 CLI（实测 relay 0.866、
        claude 0.602）。能力在 ``DefaultRouter.score`` 里是闸门不是权重，可是没有人给
        ``Task.capabilities`` 填过东西 —— 空集是任何集合的子集，人人都「有能力」。后果是
        「帮我改一下这个函数」和「今天天气怎么样」派给同一个后端，而前者那个后端连文件都
        读不了：它会写出一段看起来正确、其实没人执行的步骤。

        **调用方给的赢。** 显式带 capabilities 的 task 不被改写：那是主脑（ADR 008）将来
        要用的入口，一个会覆盖调用方判断的推断层会让那条路无法测试。

        解析器没有 ``capabilities`` 方法时什么都不做 —— 这是可选协议，注入一个只实现
        ``resolve`` 的解析器仍然合法。
        """
        if task.capabilities:
            return task
        wanted = getattr(self.resolver, "capabilities", None)
        if not callable(wanted):
            return task
        try:
            needed = frozenset(wanted(task.text))
        except Exception:  # noqa: BLE001 - 分类失败就按「谁都行」走，不能让一轮失败
            return task
        if not needed:
            return task
        return replace(task, capabilities=needed)

    @staticmethod
    def _backends(available: tuple[tuple[str, Any], Any]) -> list[str]:
        """每个被派发的后端的一句话：agent / kind / model / 端点或命令。

        **给运行日志用，不给事件用。** 事件扇出到球、到传输、到每个消费者，所以它只带
        名字与计数；这一行只到本机控制台的日志视图，而「到底是哪个模型答的」只有带上
        model 与端点才答得出 —— 使用者报「所有聊天都进了 Claude Code」时，没有任何一处
        读数能证实或否证它，这就是那次的账。

        ``check()`` 是适配器自报的形状（``http`` 报 model + endpoint，``cli`` 报 command），
        它**不打网络**。永不带凭据：适配器只报 ``token_configured`` 与 ``key_env`` 名字。
        """
        rows: list[str] = []
        for name, adapter in available:
            detail = ""
            try:
                status = adapter.check()
            except Exception:  # noqa: BLE001 - 诊断路径不能自己炸
                status = None
            if isinstance(status, Mapping):
                parts = [str(status.get("kind") or "")]
                for key in ("model", "endpoint", "command"):
                    value = str(status.get(key) or "").strip()
                    if value:
                        parts.append(f"{key}={value}")
                if status.get("available") is False:
                    parts.append("unavailable")
                detail = " ".join(part for part in parts if part)
            rows.append(f"{name}({detail})" if detail else name)
        return rows

    def _stream_agents(
        self,
        task: Task,
        plan: DispatchPlan,
        available: tuple[tuple[str, Any], ...],
        started: float,
    ) -> Iterator[AgentChunk]:
        """Open every stream, merge them, then record and cancel exactly once."""
        names = [name for name, _ in available]
        self._emit(
            "task.dispatched",
            {
                "task_id": task.id,
                "mode": plan.mode,
                "agents": list(names),
                "reason": plan.reason,
            },
        )
        streams = [adapter.stream(task) for _, adapter in available]
        merged = self.aggregator.merge(plan.mode, streams)
        saw_terminal = False
        failed_error: str | None = None
        first_chunk = True
        try:
            for chunk in merged:
                if first_chunk:
                    first_chunk = False
                    # One progress event per turn, at first output. This is the
                    # number the orb needs (time-to-first-token) and the only
                    # one a per-chunk event would add noise to -- a streaming
                    # reply would otherwise emit hundreds of validated
                    # envelopes into every log and transport.
                    #
                    # It reports the dispatched set, not the winner: the merged
                    # chunk carries no origin (``AgentChunk`` has no ``agent``
                    # field), so only the aggregator knows which stream spoke
                    # first. Naming a guess here would be worse than naming
                    # none.
                    self._emit(
                        "task.progress",
                        {
                            "task_id": task.id,
                            "agents": list(names),
                            "first_chunk_ms": self._ms_since(started),
                        },
                    )
                if chunk.kind == "done":
                    saw_terminal = True
                    failed_error = chunk.error
                yield chunk
        finally:
            elapsed = self._ms_since(started)
            # An abandoned stream never saw its ``done``; that is a failure for
            # routing purposes -- the turn produced no complete answer.
            ok = saw_terminal and not failed_error
            for name in names:
                self.router.record(name, ok=ok, elapsed_ms=elapsed)
            self._cancel_all(available, task)
            payload: dict[str, Any] = {
                "task_id": task.id,
                "mode": plan.mode,
                "agents": list(names),
                "elapsed_ms": elapsed,
            }
            if not ok:
                # Why it failed, never what was said. A stream that ends with no
                # terminating chunk is its own failure mode, distinct from one
                # that reported an error.
                payload["error"] = _public_failure_reason(
                    failed_error or "stream ended without a terminal chunk"
                )
            self._emit("task.done" if ok else "task.failed", payload)

    def _cancel_all(
        self, available: tuple[tuple[str, Any], ...], task: Task
    ) -> None:
        """Cancel every dispatched adapter. Idempotent by contract.

        Called for the winner too. ``cancel()`` must be safe after completion
        (``AgentAdapter``'s docstring says so), and tracking which adapter won
        in order to skip one call would trade a guaranteed-safe call for a
        chance of leaking the loser.
        """
        for name, adapter in available:
            canceller = getattr(adapter, "cancel", None)
            if not callable(canceller):
                continue
            try:
                canceller(task.id)
            except Exception:  # noqa: BLE001 - cleanup must not raise
                # A cancel that throws must not mask the turn's own outcome,
                # and there is nothing left to do about it either way.
                pass

    # -- reporting -----------------------------------------------------------

    def _emit(self, event_type: str, detail: Mapping[str, Any]) -> dict[str, Any]:
        """Build, validate, then hand the sink a full envelope.

        The envelope is constructed **only** in ``core.events`` -- the same rule
        the tool runner and the memory layer follow. Calling the sink with a
        bare ``(type, detail)`` pair, as this used to, meant nothing ever
        checked the type against ``contracts/agent-events.schema.json``: it
        emitted ``task.completed`` for the whole of P6 and no test could have
        caught it, because there was no validation on the path.

        Task ids, agent names, counts, timings. Never the utterance, the
        prompt, or the reply -- these events fan out to every log and
        transport, exactly like the memory and tool events (P3, P4).
        """
        event = validate_event(build_event(event_type, dict(detail)), AGENT_SCHEMA_PATH)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                # Do not turn a healthy agent response into a dispatch error
                # because the desktop/logging transport went away.
                self.sink_failures += 1
        return event

    @staticmethod
    def _ms_since(started: float) -> int:
        return int((time.monotonic() - started) * 1000)


__all__ = [
    "DispatchResult",
    "Dispatcher",
]
