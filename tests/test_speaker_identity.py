"""Who spoke: from the voiceprint gate to the tool authorisation, one line.

Before this line existed, ``core/audio/capture.py`` computed a verified speaker
and threw the name away (it passed only the score to ``on_wake``), while
``vox_plugin/plugin.py``'s own docstring said "the capture layer knows who was
verified; the plugin does not". Callers filled the gap with a constant --
``scripts/acceptance/live_conversation.py`` passed ``speaker="owner"`` -- so in
microphone mode ``shell.run``'s one credential was a string literal.

Every assertion here is about the closed direction: the identity appears only on
acceptance, disappears on everything else, and never travels in an event.

Evidence level: AUTO (stub KWS/verifier/ASR/dispatcher; no device, no model).
"""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agents.contract import AgentChunk
from core.audio import ProviderStatus, SounddeviceWakeCapture
from core.audio.speaker import VerificationResult
from core.dispatch.dispatcher import DispatchResult
from vox_plugin.plugin import VoicePlugin
from vox_plugin.runtime import VoiceRuntime


class StubKws:
    def __init__(self, hits=None) -> None:
        self.hits = list(hits or [])

    def load(self):
        return ProviderStatus(True, "stub", {"engine": "stub"})

    def create_stream(self):
        return object()

    def feed(self, stream, samples, sample_rate=16000):
        del stream, samples, sample_rate
        hits, self.hits = self.hits, []
        return [(keyword, None) for keyword in hits]

    def close(self):
        pass


class StubVerifier:
    """Returns a fixed verdict, or raises when ``explode`` is set."""

    def __init__(self, *, accepted=True, speaker="due", explode=False) -> None:
        self.result = VerificationResult(
            accepted, speaker if accepted else None, 0.91, "match" if accepted else "below"
        )
        self.speakers = ["due"]
        self.explode = explode

    def load(self):
        return ProviderStatus(True, "stub", {"dim": 512})

    def verify(self, samples, *, sample_rate=16000):
        del samples, sample_rate
        if self.explode:
            raise RuntimeError("model blew up")
        return self.result


class FakeDispatcher:
    def __init__(self, chunks=None):
        self.chunks = list(chunks or [AgentChunk(kind="text", text="ok"), AgentChunk(kind="done")])
        self.speakers = []

    def dispatch(self, task, adapters, *, speaker=None):
        self.speakers.append(speaker)
        return DispatchResult(route="agent", chunks=tuple(self.chunks), ok=True)


def block(samples: int = 160, value: float = 0.2) -> np.ndarray:
    return np.full((samples, 1), value, dtype="float32")


def wired(*, verifier=None, gate=True):
    """A capture with the gate configured, attached to a running plugin."""
    kws = StubKws(["你好问问"])
    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=160,
        require_verification=gate,
        verifier=verifier,
    )
    capture._inference_stream = kws.create_stream()
    plugin = VoicePlugin()
    plugin.start()
    plugin.attach_capture(capture)
    return plugin, capture


# ------------------------------------------------------------------ the gate


def test_an_accepted_wake_delivers_the_verified_name():
    plugin, capture = wired(verifier=StubVerifier(speaker="due"))

    capture._callback(block(), 160, None, None)

    assert plugin.verified_speaker == "due"


def test_a_rejected_wake_leaves_nobody_verified():
    plugin, capture = wired(verifier=StubVerifier(accepted=False))

    capture._callback(block(), 160, None, None)

    assert plugin.verified_speaker is None


def test_a_rejection_clears_a_previously_verified_speaker():
    """The window between a good wake and the next one must not carry authority.

    A queued utterance losing its credential is the fail-closed direction: the
    worst case is ``shell.run`` refusing something it could have run.
    """
    plugin, capture = wired(verifier=StubVerifier(speaker="due"))
    capture._callback(block(), 160, None, None)
    assert plugin.verified_speaker == "due"

    capture.verifier = StubVerifier(accepted=False)
    capture.keyword_provider.hits = ["你好问问"]
    capture._callback(block(), 160, None, None)

    assert plugin.verified_speaker is None


def test_a_verifier_that_raises_leaves_nobody_verified():
    plugin, capture = wired(verifier=StubVerifier(explode=True))

    capture._callback(block(), 160, None, None)

    assert plugin.verified_speaker is None


def test_an_ungated_wake_verifies_nobody():
    """``--no-gate`` is the escape hatch. No gate means no identity, not a default
    one -- an unguarded platform must not be able to run privileged tools."""
    plugin, capture = wired(gate=False)

    capture._callback(block(), 160, None, None)

    assert plugin.verified_speaker is None
    assert plugin.machine.state.value == "listening"  # the wake still happened


def test_a_failing_consumer_leaves_the_identity_cleared():
    """The clear call runs first, so a raising ``on_verified`` cannot leave a name.

    The capture counts the fault like any other callback error rather than letting
    it escape into the audio thread.
    """
    kws = StubKws(["你好问问"])
    calls = []

    def angry(speaker):
        calls.append(speaker)
        if speaker is not None:
            raise RuntimeError("consumer exploded")

    capture = SounddeviceWakeCapture(
        kws,
        lambda *a: None,
        blocksize=160,
        require_verification=True,
        verifier=StubVerifier(speaker="due"),
        on_verified=angry,
    )
    capture._inference_stream = kws.create_stream()

    capture._callback(block(), 160, None, None)

    assert calls == [None, "due"], "cleared first, then the name"
    assert capture.callback_errors == 1


def test_detaching_the_capture_clears_the_identity():
    plugin, capture = wired(verifier=StubVerifier(speaker="due"))
    capture._callback(block(), 160, None, None)
    assert plugin.verified_speaker == "due"

    plugin.attach_capture(None)

    assert plugin.verified_speaker is None


def test_set_verified_speaker_normalises_empty_to_none():
    plugin = VoicePlugin()
    plugin.set_verified_speaker("")
    assert plugin.verified_speaker is None


# ------------------------------------------------------------------ privacy


def test_the_identity_never_travels_in_an_event():
    """Events fan out to every log and transport. ``score`` is the diagnostic;
    the name adds nothing it does not already carry."""
    plugin, capture = wired(verifier=StubVerifier(speaker="due"))

    capture._callback(block(), 160, None, None)

    assert "due" not in repr(plugin.events)


def test_a_rejection_event_carries_no_identity():
    plugin, capture = wired(verifier=StubVerifier(accepted=False, speaker="due"))

    capture._callback(block(), 160, None, None)

    rejected = [e for e in plugin.events if e["type"] == "wake.rejected"]
    assert rejected and "due" not in repr(rejected)


# --------------------------------------------------------- the authorisation


def test_a_microphone_runtime_authorises_as_the_verified_speaker():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.dispatcher = dispatcher = FakeDispatcher()
    runtime.adapters = {}
    _plugin, capture = wired(verifier=StubVerifier(speaker="due"))
    runtime.attach_microphone(capture)
    capture._callback(block(), 160, None, None)

    runtime.say("读一下 README")

    assert dispatcher.speakers == ["due"]


def test_a_microphone_runtime_ignores_a_constructor_speaker():
    """This is the substitution that made the credential a string literal.

    With a microphone attached the gate is the only source of identity, including
    when its answer is "nobody".
    """
    runtime = VoiceRuntime(speaker="owner", with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.dispatcher = dispatcher = FakeDispatcher()
    runtime.adapters = {}
    _plugin, capture = wired(verifier=StubVerifier(accepted=False))
    runtime.attach_microphone(capture)
    capture._callback(block(), 160, None, None)

    runtime.say("删掉那个目录")

    assert dispatcher.speakers == [None]
    assert runtime.effective_speaker is None


def test_without_a_microphone_the_constructor_value_still_stands():
    """The typed path is a caller asserting it verified the user some other way.
    That entry point predates this wiring and is unchanged."""
    runtime = VoiceRuntime(speaker="due", with_desktop=False, with_memory=False)
    runtime._started = True
    runtime.dispatcher = dispatcher = FakeDispatcher()
    runtime.adapters = {}

    runtime.say("读一下 README")

    assert dispatcher.speakers == ["due"]
    assert runtime.effective_speaker == "due"


def test_describe_reports_where_the_identity_came_from():
    runtime = VoiceRuntime(speaker="due", with_desktop=False, with_memory=False)
    assert runtime.describe()["gate_source"] == "caller"

    _plugin, capture = wired(verifier=StubVerifier(speaker="due"))
    runtime.attach_microphone(capture)

    described = runtime.describe()
    assert described["gate_source"] == "microphone"
    assert described["speaker_verified"] is False, "no wake has been accepted yet"


def test_closing_the_runtime_drops_the_identity():
    runtime = VoiceRuntime(with_desktop=False, with_memory=False)
    runtime._started = True
    _plugin, capture = wired(verifier=StubVerifier(speaker="due"))
    runtime.attach_microphone(capture)
    capture._callback(block(), 160, None, None)
    assert runtime.effective_speaker == "due"

    runtime.close()

    assert runtime.plugin.verified_speaker is None
