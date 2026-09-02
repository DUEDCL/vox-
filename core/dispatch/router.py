"""Five-dimension routing (ADR 005).

Capability, cost, latency, historical success rate, current load. The first
three are what an agent declares about itself in ``config/agents.toml``; the
fourth is what the platform has observed (ADR 004's long-term layer); the fifth
is what is happening right now.

Capability is a **gate, not a weight**. ``score()`` reports an incapable agent
with its dimensions broken out, because "why did claude lose" deserves an
answer -- but ``plan()`` will not route to it. A weight would let an agent that
declared it cannot do vision win a vision task by being cheap and fast, and the
declaration would have been decorative.

Scoring is separated from planning for the same reason: a caller may want to
see the ranking without dispatching anything. ``plan()`` is the one that has
side effects (it takes out a load slot); ``score()`` is pure.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.agents.contract import AgentDescriptor, Task
from core.dispatch.contract import DISPATCH_MODES, DispatchPlan, RouteScore

#: Weights over the four scored dimensions -- capability is not among them; it
#: gates. They sum to 1.0, so a free, instant, never-failed, idle agent scores
#: exactly 1.0 and the numbers in ``RouteScore.total`` are readable as such.
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "cost": 0.35,
    "latency": 0.35,
    "success": 0.20,
    "load": 0.10,
}

#: ``AgentDescriptor.cost`` is a 1-5 declaration (``contracts/agents.schema.json``).
COST_MIN, COST_MAX = 1, 5

#: Latency normalisation window. 1 s or better scores 1.0; 10 s or worse scores
#: 0.0. Beyond ten seconds a voice turn has already failed the user, so grading
#: 30 s against 60 s would be measuring degrees of too-late.
LATENCY_MIN_MS, LATENCY_MAX_MS = 1000, 10_000

#: Load normalisation. Ten concurrent turns on one agent is the point where its
#: queueing dominates its declared latency.
LOAD_SATURATION = 10

#: Agents started in ``race`` mode. Two, not three: each is a real subprocess
#: with a model behind it, and the third contributes little once the top two
#: are already close (ADR 005's latency-vs-resources trade).
RACE_WIDTH = 2

#: Score for an agent with no recorded outcomes. Deliberately mid-range: zero
#: would make a never-tried agent lose to one that has failed every turn, which
#: is how a fleet silently collapses onto whichever agent happened to run first.
UNKNOWN_SUCCESS = 0.5


class DefaultRouter:
    """Rank agents, plan dispatch, and absorb outcomes.

    Breaker and memory are injected rather than constructed. The breaker holds
    process-local health; the memory layer holds cross-restart statistics. With
    neither attached the router still works on declarations alone, which is
    what a first run looks like before anything has been observed.
    """

    def __init__(
        self,
        agents: Sequence[AgentDescriptor] = (),
        *,
        breaker: Any = None,
        memory_recaller: Any = None,
        memory_writer: Any = None,
        weights: Mapping[str, float] | None = None,
    ) -> None:
        self.agents: dict[str, AgentDescriptor] = {
            descriptor.name: descriptor for descriptor in agents
        }
        self.breaker = breaker
        self.memory_recaller = memory_recaller
        self.memory_writer = memory_writer
        self.weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        self._load: dict[str, int] = {}
        #: 主机上装没装这个后端。**和熔断器是两件事**：熔断记的是「它一直失败」（会自愈），
        #: 这里记的是「这台机器上根本没有它」（只有重新检查才会变）。
        #:
        #: 为什么需要它：``config/agents.toml`` 刻意**保留**命令不在 PATH 上的条目 ——
        #: 丢掉它会让「少一个 agent」和「配错一个 agent」无法区分（见 registry.py 的开头）。
        #: 代价是路由会选中一个跑不起来的后端：实测这台机器上 `claude` 装在
        #: `%APPDATA%\npm` 而那个目录不在 PATH 上，于是「帮我改一下这个函数」被能力闸门
        #: 正确地送给了 claude，然后**整轮失败** —— 使用者听到的是一句「agent 报告失败」。
        #:
        #: 保留条目 + 路由时跳过，两件事合起来才对：清单里仍然看得见它、也看得见原因，
        #: 而这一轮不会被派到它身上。
        self._unavailable: dict[str, str] = {}

    def register(self, descriptor: AgentDescriptor) -> None:
        """Add or replace one agent. Its load and breaker state are untouched."""
        self.agents[descriptor.name] = descriptor

    def mark_unavailable(self, agent: str, reason: str = "") -> None:
        """这台机器上没有这个后端（命令不在 PATH、模型没装）。路由从此跳过它。

        由启动时的 ``adapter.check()`` 结果驱动 —— 见 ``VoiceRuntime._open_agents``。
        幂等，且**只影响路由**：``describe()`` 与就绪清单照常列出这个 agent 和原因，
        因为「配了但这台机器上没有」和「没配」是使用者要能分开看的两件事。
        """
        self._unavailable[agent] = reason or "unavailable"

    def mark_available(self, agent: str) -> None:
        """撤掉上面那条标记（重新检查发现它回来了）。"""
        self._unavailable.pop(agent, None)

    def allows(self, agent: str) -> bool:
        """路由现在愿不愿意选这个 agent。熔断与「主机上没有」两条都要过。"""
        if agent in self._unavailable:
            return False
        if self.breaker is not None and not self.breaker.allows(agent):
            return False
        return True

    # -- scoring -------------------------------------------------------------

    def score(self, task: Task) -> tuple[RouteScore, ...]:
        """Rank every non-tripped agent. Explainability is the point.

        Incapable agents are included with ``capability=0.0`` and a reason
        naming what they lack. Tripped agents are excluded outright -- a
        breaker that is open is not a ranking, it is a removal.
        """
        scores: list[RouteScore] = []
        for name, descriptor in self.agents.items():
            if not self.allows(name):
                continue
            capable = task.capabilities <= descriptor.capabilities
            cost = self._cost_score(descriptor)
            latency = self._latency_score(descriptor)
            success = self._success_score(name)
            load = self._load_score(name)
            total = (
                self.weights.get("cost", 0.0) * cost
                + self.weights.get("latency", 0.0) * latency
                + self.weights.get("success", 0.0) * success
                + self.weights.get("load", 0.0) * load
            )
            if capable:
                reason = f"total {total:.3f}"
            else:
                missing = sorted(task.capabilities - descriptor.capabilities)
                reason = f"missing {', '.join(missing)}"
                total = 0.0
            scores.append(
                RouteScore(
                    agent=name,
                    total=round(total, 4),
                    capability=1.0 if capable else 0.0,
                    cost=round(cost, 4),
                    latency=round(latency, 4),
                    success=round(success, 4),
                    load=round(load, 4),
                    reason=reason,
                )
            )
        # Name breaks ties so the same fleet plans identically every run; an
        # order that depends on dict insertion makes routing tests flaky.
        return tuple(
            sorted(scores, key=lambda entry: (-entry.total, entry.agent))
        )

    def _cost_score(self, descriptor: AgentDescriptor) -> float:
        clamped = max(COST_MIN, min(COST_MAX, descriptor.cost))
        return (COST_MAX - clamped) / (COST_MAX - COST_MIN)

    def _latency_score(self, descriptor: AgentDescriptor) -> float:
        clamped = max(LATENCY_MIN_MS, min(LATENCY_MAX_MS, descriptor.latency_ms))
        return (LATENCY_MAX_MS - clamped) / (LATENCY_MAX_MS - LATENCY_MIN_MS)

    def _success_score(self, agent: str) -> float:
        """Observed success rate, or ``UNKNOWN_SUCCESS`` when nothing is known.

        ``MemoryRecaller.success_rate()`` returns ``rate=None`` for an agent
        with no observations rather than 0.0 -- no data is not the same claim
        as total failure, and this is the consumer that depends on it.
        """
        if self.memory_recaller is None:
            return UNKNOWN_SUCCESS
        try:
            stats = self.memory_recaller.success_rate(agent)
        except Exception:  # noqa: BLE001 - statistics must not break routing
            return UNKNOWN_SUCCESS
        rate = stats.get("rate") if isinstance(stats, Mapping) else None
        if rate is None:
            return UNKNOWN_SUCCESS
        return max(0.0, min(1.0, float(rate)))

    def _load_score(self, agent: str) -> float:
        return max(0.0, 1.0 - self._load.get(agent, 0) / LOAD_SATURATION)

    # -- planning ------------------------------------------------------------

    def plan(self, task: Task) -> DispatchPlan:
        """Pick mode and agents, and take out a load slot for each.

        Every returned agent is capable, untripped, and has had its load
        incremented. ``record()`` releases the slot; so does ``release()`` when
        the dispatcher finds no adapter for a planned name.
        """
        mode = task.mode if task.mode in DISPATCH_MODES else "single"
        eligible = [entry for entry in self.score(task) if entry.capability == 1.0]
        if not eligible:
            return DispatchPlan(mode=mode, agents=(), reason=self._why_none(task))
        if mode == "single":
            picked = eligible[:1]
            reason = f"{picked[0].agent} scored {picked[0].total:.3f}"
        elif mode == "race":
            picked = eligible[:RACE_WIDTH]
            reason = "racing " + ", ".join(entry.agent for entry in picked)
        else:  # fanout -- every eligible agent, by explicit request only
            picked = eligible
            reason = f"fanning out to {len(picked)} agents"
        names = tuple(entry.agent for entry in picked)
        for name in names:
            self._load[name] = self._load.get(name, 0) + 1
        return DispatchPlan(mode=mode, agents=names, reason=reason)

    def _why_none(self, task: Task) -> str:
        """The reason no agent was eligible -- the dispatcher speaks this."""
        if not self.agents:
            return "no agents configured"
        if all(not self.allows(name) for name in self.agents):
            # 「这台机器上没装」要和「一直失败被熔断」分开说：前者要去装或改 PATH，
            # 后者等它自愈或去看它为什么失败。合成一句话的话两种都查不动。
            missing = sorted(name for name in self.agents if name in self._unavailable)
            if missing and len(missing) == len(self.agents):
                return "no agent is installed on this machine: " + ", ".join(
                    f"{name} ({self._unavailable[name]})" for name in missing
                )
            return "all agents tripped"
        if task.capabilities:
            wanted = ", ".join(sorted(task.capabilities))
            blocked = sorted(
                name
                for name, descriptor in self.agents.items()
                if name in self._unavailable and task.capabilities <= descriptor.capabilities
            )
            if blocked:
                # **这一条是「能力闸门 + 后端没装」那个组合的唯一线索。** 闸门把这一句
                # 正确地判给了唯一能做的后端，而那个后端这台机器上没有 —— 不说出来的话
                # 读到的是「no agent has code」，看起来像配置里少写了一个能力词。
                return f"no available agent has {wanted}; " + ", ".join(
                    f"{name} has it but is {self._unavailable[name]}" for name in blocked
                )
            return f"no agent has {wanted}"
        return "no agents available"

    # -- outcomes ------------------------------------------------------------

    def record(self, agent: str, *, ok: bool, elapsed_ms: int) -> None:
        """Feed one outcome back into the success-rate and breaker state."""
        self.release(agent)
        if self.breaker is not None:
            self.breaker.record(agent, ok=ok)
        if self.memory_writer is None:
            return
        try:
            self.memory_writer.record_agent_outcome(
                agent, ok=ok, latency_ms=elapsed_ms
            )
        except Exception:  # noqa: BLE001 - an audit write must not fail a turn
            # Same posture as the memory wiring in ``vox_plugin.plugin``:
            # statistics are an enhancement to a turn, never a precondition
            # for one. A locked database must not be able to end a
            # conversation.
            pass

    def release(self, agent: str) -> None:
        """Give back a load slot without recording an outcome.

        For agents that were planned but never dispatched -- no adapter was
        open for the name. That is a configuration mismatch, and charging it to
        the agent's success rate would eventually route around a backend that
        never actually failed.
        """
        current = self._load.get(agent)
        if current is None:
            return
        if current <= 1:
            del self._load[agent]
        else:
            self._load[agent] = current - 1

    def describe(self) -> dict[str, Any]:
        """Routing state for diagnostics. No task text, ever."""
        return {
            "agents": sorted(self.agents),
            "weights": dict(self.weights),
            "in_flight": dict(sorted(self._load.items())),
            "breaker": self.breaker is not None,
            "memory": self.memory_recaller is not None,
            # 「配了但这台机器上没有」必须能被读出来 —— 它是路由结果的一半原因。
            "unavailable": dict(sorted(self._unavailable.items())),
        }


__all__ = [
    "COST_MAX",
    "COST_MIN",
    "DEFAULT_WEIGHTS",
    "LATENCY_MAX_MS",
    "LATENCY_MIN_MS",
    "LOAD_SATURATION",
    "RACE_WIDTH",
    "UNKNOWN_SUCCESS",
    "DefaultRouter",
]
