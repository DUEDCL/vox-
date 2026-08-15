"""Result aggregation across concurrent agent streams (ADR 005).

``single`` yields one stream through unchanged. ``race`` starts every stream and
keeps the first one to produce a chunk, signalling the rest to stop. ``fanout``
runs all of them and concatenates what they produced, followed by one synthetic
``done`` carrying the aggregate timing.

Concurrency lives here rather than in the dispatcher because the contract puts
merging here: ``merge(mode, streams)`` receives iterators, and for ``race`` the
losers must be *cancelled rather than drained* -- that is a property of how the
merge consumes them, not of how the dispatcher chose them.

Threads rather than asyncio: every adapter written so far is a synchronous
generator over blocking I/O (a subprocess pipe, an HTTP round trip). Wrapping
them in an event loop would mean either one executor thread per stream -- the
same threads, one layer down -- or rewriting all of P5.

**Loser cleanup is best-effort at this layer, and that is a real limitation.**
The stop event is checked between chunks, so a loser blocked inside ``next()``
on a slow pipe finishes its current read before noticing. Prompt shutdown is
the adapter's ``cancel()``, which only the dispatcher can reach; it calls it for
every dispatched agent once the merged stream ends. Calling ``close()`` from
this side would be worse than useless -- a generator that is currently executing
raises ``ValueError`` rather than stopping -- so each drain thread closes its
own stream, which is the only thread allowed to.
"""

from __future__ import annotations

import threading
from queue import Queue
from typing import Any, Iterator, Sequence

from core.agents.contract import AgentChunk

#: Streams draining at once. Each is a thread plus, for ``cli``, a subprocess;
#: `models/` already costs ~451 MB of RAM, so this is deliberately small.
DEFAULT_MAX_CONCURRENT = 3


class DefaultAggregator:
    """Merge concurrent agent streams. One instance is reusable across turns."""

    def __init__(self, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self.max_concurrent = int(max_concurrent)

    def merge(
        self, mode: str, streams: Sequence[Iterator[AgentChunk]]
    ) -> Iterator[AgentChunk]:
        """Yield a single increment stream. Losers are stopped, not drained."""
        pending = list(streams)
        if not pending:
            return iter(())
        if len(pending) == 1:
            return self._passthrough(pending[0])
        if mode == "race":
            return self._race(pending)
        if mode == "fanout":
            return self._fanout(pending)
        # ``single`` with several streams, or a mode this class does not know:
        # take the first and stop the rest rather than silently fanning out.
        return self._race(pending)

    # -- one stream ----------------------------------------------------------

    def _passthrough(self, stream: Iterator[AgentChunk]) -> Iterator[AgentChunk]:
        """No thread, no queue: a single stream is already the merged stream.

        Failures stay failures here. The adapters turn every error into a
        terminating ``done`` carrying ``error`` (P5), so an exception escaping
        this loop is a bug in an adapter rather than an agent failing, and
        swallowing it would hide the bug.
        """
        yield from stream

    # -- draining ------------------------------------------------------------

    def _drain(
        self,
        index: int,
        stream: Iterator[AgentChunk],
        out: "Queue[tuple[int, AgentChunk | None]]",
        stop: threading.Event,
        slot: threading.Semaphore,
    ) -> None:
        """Push one stream's chunks onto the shared queue, then a ``None``.

        The trailing ``None`` is how the consumer counts finished streams; a
        ``done`` chunk cannot serve that purpose, because a stream that raises
        never emits one.
        """
        with slot:
            try:
                for chunk in stream:
                    out.put((index, chunk))
                    if stop.is_set():
                        break
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                # An adapter that raises instead of yielding ``done`` is a bug,
                # but one buggy agent must not take the whole turn down.
                out.put(
                    (
                        index,
                        AgentChunk(
                            kind="done", error=f"{type(exc).__name__}: {exc}"
                        ),
                    )
                )
            finally:
                # Only this thread may close this generator.
                closer = getattr(stream, "close", None)
                if callable(closer):
                    closer()
                out.put((index, None))

    def _start(
        self, streams: Sequence[Iterator[AgentChunk]]
    ) -> tuple["Queue[tuple[int, AgentChunk | None]]", threading.Event, list[Any]]:
        out: "Queue[tuple[int, AgentChunk | None]]" = Queue()
        stop = threading.Event()
        slot = threading.Semaphore(self.max_concurrent)
        threads: list[Any] = []
        for index, stream in enumerate(streams):
            thread = threading.Thread(
                target=self._drain,
                args=(index, stream, out, stop, slot),
                name=f"dispatch-drain-{index}",
                # Daemon: a hung agent must not keep the process alive. The
                # dispatcher's cancel() is what actually stops it.
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        return out, stop, threads


    # -- race ----------------------------------------------------------------

    def _race(
        self, streams: Sequence[Iterator[AgentChunk]]
    ) -> Iterator[AgentChunk]:
        """First stream to produce a chunk wins; the rest are stopped.

        The winner is decided on the **first chunk**, not the first completion.
        In a voice interface the perceived latency is the first spoken token,
        so an agent that starts talking sooner wins even if it finishes later.

        A stream that ends without ever producing a chunk cannot win. If every
        stream does that, the merged stream is empty -- which is what the
        dispatcher sees as "no agent produced anything".
        """
        out, stop, _threads = self._start(streams)
        finished: set[int] = set()
        total = len(streams)
        winner: int | None = None
        while len(finished) < total:
            index, chunk = out.get()
            if chunk is None:
                finished.add(index)
                if winner is not None and index == winner:
                    return
                continue
            if winner is None:
                winner = index
                # Losers stop at their next chunk boundary; the dispatcher's
                # cancel() is what interrupts one blocked mid-read.
                stop.set()
            if index == winner:
                yield chunk

    # -- fanout --------------------------------------------------------------

    def _fanout(
        self, streams: Sequence[Iterator[AgentChunk]]
    ) -> Iterator[AgentChunk]:
        """Run all streams, then concatenate by agent order.

        Buffered rather than interleaved: chunks from several agents arriving
        in completion order would produce spliced half-sentences, and this mode
        exists for cross-checking answers, not for latency. That buffering is
        exactly why ``fanout`` is never the voice default -- first token waits
        for the slowest agent (ADR 005).

        Errors do not propagate. A ``done`` chunk carrying ``error`` marks that
        agent's contribution as failed and drops it; the remaining agents still
        answer. If every one fails, only the synthetic ``done`` is emitted, and
        it carries the first error so the caller can say why.
        """
        out, stop, _threads = self._start(streams)
        collected: dict[int, list[AgentChunk]] = {i: [] for i in range(len(streams))}
        failed: set[int] = set()
        first_error: str | None = None
        elapsed_ms = 0
        tokens = 0
        finished: set[int] = set()
        while len(finished) < len(streams):
            index, chunk = out.get()
            if chunk is None:
                finished.add(index)
                continue
            if chunk.kind == "done":
                # Per-agent ``done`` is folded into the aggregate, never
                # forwarded: exactly one ``done`` terminates a merged stream.
                if chunk.error:
                    failed.add(index)
                    if first_error is None:
                        first_error = chunk.error
                elapsed_ms = max(elapsed_ms, chunk.elapsed_ms or 0)
                tokens += chunk.tokens or 0
                continue
            collected[index].append(chunk)
        stop.set()
        for index in range(len(streams)):
            if index in failed:
                continue
            yield from collected[index]
        yield AgentChunk(
            kind="done",
            elapsed_ms=elapsed_ms,
            tokens=tokens or None,
            error=first_error if len(failed) == len(streams) else None,
        )


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DefaultAggregator",
]
