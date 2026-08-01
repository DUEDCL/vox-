"""Fixed-size in-memory audio ring buffer.

The speaker gate has to look at the audio *preceding* the wake-word hit: by the
time the keyword spotter reports a match, the words it matched are already in
the past. A small ring buffer is the cheapest way to keep them reachable.

Design red line 1 lands here literally. This class holds a single preallocated
array, exposes it only as a copy, and has no code path that touches the
filesystem, a socket, or any other process. ``tests/test_speaker_privacy.py``
asserts that property rather than trusting this docstring.

Three seconds at 16 kHz float32 is about 192 KB -- small enough that the buffer
is allocated once at construction and never grown.
"""

from __future__ import annotations

from typing import Any

DEFAULT_SECONDS = 3.0


class AudioRingBuffer:
    """Keep the most recent ``seconds`` of mono audio and nothing else."""

    def __init__(self, *, sample_rate: int = 16000, seconds: float = DEFAULT_SECONDS) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if seconds <= 0:
            raise ValueError("seconds must be positive")
        self.sample_rate = sample_rate
        self.seconds = float(seconds)
        self.capacity = int(sample_rate * seconds)
        self._numpy = _import_numpy()
        self._buffer = self._numpy.zeros(self.capacity, dtype="float32")
        self._write = 0
        self._filled = 0

    def __len__(self) -> int:
        return self._filled

    @property
    def filled_seconds(self) -> float:
        return self._filled / float(self.sample_rate)

    def write(self, samples: Any) -> None:
        """Append one capture chunk, overwriting the oldest audio when full."""
        block = self._numpy.asarray(samples, dtype="float32").reshape(-1)
        size = block.shape[0]
        if size == 0:
            return
        if size >= self.capacity:
            # A chunk larger than the whole window: only its tail can survive.
            self._buffer[:] = block[-self.capacity :]
            self._write = 0
            self._filled = self.capacity
            return
        end = self._write + size
        if end <= self.capacity:
            self._buffer[self._write : end] = block
        else:
            split = self.capacity - self._write
            self._buffer[self._write :] = block[:split]
            self._buffer[: end - self.capacity] = block[split:]
        self._write = end % self.capacity
        self._filled = min(self.capacity, self._filled + size)

    def snapshot(self, seconds: float | None = None) -> Any:
        """Return a copy of the most recent audio, oldest sample first.

        A copy, not a view: the caller feeds this to the embedding extractor
        while the capture callback keeps writing, and a view would tear.
        """
        wanted = self._filled if seconds is None else min(self._filled, int(self.sample_rate * seconds))
        if wanted <= 0:
            return self._numpy.zeros(0, dtype="float32")
        start = (self._write - wanted) % self.capacity
        if start + wanted <= self.capacity:
            return self._buffer[start : start + wanted].copy()
        head = self.capacity - start
        return self._numpy.concatenate(
            (self._buffer[start:], self._buffer[: wanted - head])
        ).astype("float32")

    def clear(self) -> None:
        """Drop the retained audio. Called after every gate decision."""
        self._buffer[:] = 0.0
        self._write = 0
        self._filled = 0


def _import_numpy() -> Any:
    import numpy  # local import keeps package import free of hard requirements

    return numpy
