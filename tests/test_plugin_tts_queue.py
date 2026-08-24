"""Multi-segment TTS queueing in the turn path (release gap: long replies).

``split_speech`` cuts a reply into sentences, ``complete_turn`` emits one
``tts.chunk`` per sentence and drains them through ``speak_segments`` when the
engine supports it, and ``stop()`` drops whatever was still queued. A barge-in
mid-synthesis must not open audio for the cancelled remainder.

Evidence level: AUTO (fake/stub engines, no speaker, no model).
"""

from __future__ import annotations

import numpy as np

from core.audio.tts import SherpaTtsProvider, TtsAudio
from core.state import VoiceState
from vox_plugin import VoicePlugin
from vox_plugin.plugin import split_speech


# -- split_speech -------------------------------------------------------------


def test_sentences_split_on_cjk_enders():
    assert split_speech("你好。我在忙，稍后说！真的？") == [
        "你好。",
        "我在忙，稍后说！",
        "真的？",
]


def test_ascii_sentence_punctuation_splits_with_a_decimal_guard():
    assert split_speech("Hello there. How are you?") == ["Hello there.", "How are you?"]
    assert split_speech("价格是3.14元，含税。") == ["价格是3.14元，含税。"]


def test_newlines_and_enderless_tails_are_handled():
    assert split_speech("第一行\n第二行。") == ["第一行", "第二行。"]
    assert split_speech("好的") == ["好的"]


def test_trailing_punctuation_run_folds_into_its_sentence():
    assert split_speech("什么？！…") == ["什么？！…"]


def test_empty_text_yields_no_segments():
    assert split_speech("") == []
    assert split_speech("  \n ") == []


# -- complete_turn over the queue ---------------------------------------------


class FakeTts:
    def __init__(self, raise_on_second: bool = False) -> None:
        self.spoken: list[str] = []
        self.raise_on_second = raise_on_second

    def speak(self, text: str) -> None:
        if self.raise_on_second and len(self.spoken) == 1:
            raise RuntimeError("audio device gone mid-reply")
        self.spoken.append(text)


class BatchTts:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def speak_segments(self, texts):
        self.batches.append(list(texts))


class RecordingWriter:
    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    def write_turn(self, text, *, role="user"):
        self.turns.append((role, text))


def ready_plugin(tts=None, writer=None) -> VoicePlugin:
    plugin = VoicePlugin()
    plugin.start()
    plugin.wake_detected("wake", 0.9)
    plugin.submit_text("你好")
    if tts is not None:
        plugin.attach_tts(tts)
    if writer is not None:
        plugin.attach_memory(writer=writer)
    return plugin


def test_complete_turn_emits_one_chunk_per_sentence():
    plugin = ready_plugin()

    events = plugin.complete_turn("第一句。第二句！第三句没有标点")

    types = [e["type"] for e in events]
    assert types == [
        "llm.delta",
        "tts.chunk",
        "tts.chunk",
        "tts.chunk",
        "state.changed",
        "turn.done",
        "state.changed",
    ]
    chunks = [e for e in events if e["type"] == "tts.chunk"]
    assert [c["payload"]["index"] for c in chunks] == [0, 1, 2]
    assert [c["payload"]["text"] for c in chunks] == [
        "第一句。",
        "第二句！",
        "第三句没有标点",
    ]


def test_reply_is_spoken_segment_by_segment_through_legacy_engines():
    tts = FakeTts()
    plugin = ready_plugin(tts)

    plugin.complete_turn("第一句。第二句！")

    assert tts.spoken == ["第一句。", "第二句！"]


def test_batch_engines_receive_the_whole_ordered_queue():
    tts = BatchTts()
    plugin = ready_plugin(tts)

    plugin.complete_turn("第一句。第二句！")

    assert tts.batches == [["第一句。", "第二句！"]]


def test_midqueue_failure_still_finishes_the_turn():
    plugin = ready_plugin(FakeTts(raise_on_second=True))

    plugin.complete_turn("第一句。第二句！")

    assert plugin.machine.state == VoiceState.LISTENING


def test_memory_stores_the_full_reply_not_the_segments():
    writer = RecordingWriter()
    plugin = ready_plugin(FakeTts(), writer)

    plugin.complete_turn("第一句。第二句！")

    assert writer.turns == [("assistant", "第一句。第二句！")]


def test_legacy_drain_honours_a_cancellation_marker():
    class CancellableFake(FakeTts):
        def __init__(self) -> None:
            super().__init__()
            self._stopped = False

        def is_stopped(self) -> bool:
            return self._stopped

        def speak(self, text: str) -> None:
            super().speak(text)
            if len(self.spoken) == 1:
                self._stopped = True  # barge-in landed after sentence one

    tts = CancellableFake()
    plugin = ready_plugin(tts)

    plugin.complete_turn("第一句。第二句！第三句！")

    assert tts.spoken == ["第一句。"]
    assert plugin.machine.state == VoiceState.LISTENING


# -- provider-level queue drain and cancellation -------------------------------


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[int] = []
        self.stops = 0

    def play(self, samples, sample_rate, *, blocking=True):
        self.played.append(len(samples))

    def stop(self):
        self.stops += 1


class StubTts(SherpaTtsProvider):
    """Real queue logic, synthetic audio: no model, no device."""

    def __init__(self, player: FakePlayer, stop_during_second_synth: bool = False):
        self.playback = player
        self._stopped = False
        self.stop_during_second_synth = stop_during_second_synth
        self.synthesized: list[str] = []

    def synthesize(self, text, *, speaker_id=None, speed=None):
        self.synthesized.append(text)
        if self.stop_during_second_synth and len(self.synthesized) == 2:
            self.stop()  # barge-in thread fires while sentence two renders
        return TtsAudio(samples=np.zeros(4, dtype=np.float32), sample_rate=16000, elapsed_ms=1)


def test_speak_segments_plays_every_sentence_in_order():
    player = FakePlayer()
    tts = StubTts(player)

    spoken = tts.speak_segments(["一。", "二。", "三。"])

    assert tts.synthesized == ["一。", "二。", "三。"]
    assert player.played == [4, 4, 4]
    assert len(spoken) == 3


def test_stop_during_synthesis_drops_the_rest_without_opening_audio():
    player = FakePlayer()
    tts = StubTts(player, stop_during_second_synth=True)

    spoken = tts.speak_segments(["一。", "二。", "三。"])

    # Sentence two rendered but its audio never opened; three was never built.
    assert tts.synthesized == ["一。", "二。"]
    assert player.played == [4]
    assert len(spoken) == 1
    assert tts.is_stopped() is True
    assert player.stops == 1


def test_provider_stop_flag_clears_when_a_new_utterance_starts():
    player = FakePlayer()
    tts = StubTts(player)

    tts.stop()
    assert tts.is_stopped() is True

    # Entry resets the flag on purpose: a cancel that lands while THINKING
    # never reaches complete_turn, so there is no stale-cancel race at entry.
    spoken = tts.speak_segments(["二。"])
    assert len(spoken) == 1
    assert tts.is_stopped() is False
