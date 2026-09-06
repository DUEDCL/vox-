"""Realtime microphone capture with the speaker gate wired in.

This is the only module in the project that opens a capture device, and the only
one that decides whether a wake-word hit is allowed to reach the platform. Both
facts are deliberate: red line 1 (no audio persisted or uploaded) and the
fail-closed speaker gate each need exactly one enforcement point.

Gate placement follows ADR 002: verification happens at the KWS hit, against the
ring buffer, *before* anything observable happens. Verifying the first recognised
sentence instead would mean an unauthorised speaker has already seen the orb.
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from .base import ProviderUnavailable
from .kws import SherpaKeywordProvider
from .ring import AudioRingBuffer


class SounddeviceWakeCapture:
    """Optional realtime microphone adapter around ``sounddevice.InputStream``.

    ``require_verification`` defaults to ``True`` and is fail-closed: with it on,
    ``start()`` refuses to open the device unless a usable verifier with at least
    one enrolled speaker is attached. A gate that silently degrades to "anyone may
    wake it" is worse than no gate, because it advertises protection it is not
    providing.
    """

    def __init__(
        self,
        keyword_provider: SherpaKeywordProvider,
        on_wake: Any,
        *,
        sample_rate: int = 16000,
        blocksize: int = 1600,
        device: int | str | None = None,
        speech_gate: Any = None,
        verifier: Any = None,
        on_reject: Any = None,
        require_verification: bool = True,
        buffer_seconds: float = 3.0,
        verify_seconds: float = 1.5,
        asr_provider: Any = None,
        on_recognized: Any = None,
        on_verified: Any = None,
        on_input_silent: Any = None,
        on_kws_hit: Any = None,
        auto_gain: Any = None,
        silent_peak: float = 1e-4,
        silent_grace_s: float = 4.0,
        listen_grace_s: float = 8.0,
    ) -> None:
        self.keyword_provider = keyword_provider
        self.on_wake = on_wake
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = device
        self.speech_gate = speech_gate
        self.verifier = verifier
        self.on_reject = on_reject
        self.require_verification = require_verification
        self.verify_seconds = verify_seconds
        #: Where the gate's verdict on *who* spoke is delivered. This is separate
        #: from ``on_wake`` because the identity must not travel with the wake
        #: event: events fan out to every log and transport, and a speaker name is
        #: personal data. It is also why ``on_wake``'s signature is unchanged --
        #: this is an addition, not a break.
        #:
        #: The contract is one call per wake attempt with ``None`` (cleared) and a
        #: second call with a name only when the gate accepted. A consumer that
        #: only ever assigns therefore cannot hold a stale identity: a failure in
        #: the second call leaves ``None`` standing, which is the closed answer.
        self.on_verified = on_verified
        #: Optional streaming ASR for the listening phase after a wake. With
        #: neither ``asr_provider`` nor ``on_recognized`` the capture stays in
        #: wake-only mode, exactly as before.
        self.asr_provider = asr_provider
        self.on_recognized = on_recognized
        self._listening = False
        self._asr_stream: Any = None
        self._ring = AudioRingBuffer(sample_rate=sample_rate, seconds=buffer_seconds)
        self._stream: Any = None
        self._inference_stream: Any = None
        self._keyword_provider_loaded = False
        self._asr_provider_loaded = False
        self._verifier_loaded = False
        self._callback_faulted = False
        #: Count only business/callback exceptions; never retain their messages.
        self.callback_errors = 0
        self.last_callback_error: str | None = None

        #: --- 死麦克风检测 -------------------------------------------------
        #:
        #: **为什么需要它。** Windows 上一个被静音、被隐私设置拒绝、或者根本不在用的输入
        #: 设备**不会报错** —— 它照常打开、回调照常以正确速率触发、每一块样本全是零。
        #: 于是 KWS 永远不命中，而每一层都报告自己健康。实测（2026-08-29，本机默认设备
        #: `麦克风阵列 (Realtek(R) Audio)`）：1.2 秒采集的 peak 是 **0.00003**，而同一
        #: 时刻另一个设备（耳机）是 **0.027**。前者是数值噪声，不是房间。
        #:
        #: 那次的表现是「自定义唤醒词唤不醒」，于是词表、阈值、音素、声纹阈值被逐个怀疑
        #: 了好几轮 —— 而没有一层坏。**一个静默失败的输入设备比一个打不开的设备糟得多**，
        #: 所以这里把它变成可观测的事实。
        #:
        #: 判据是 peak 而不是 RMS：一段正常的静音房间 RMS 也很低，但 peak 会有噪声底。
        #: 全零的设备两个都没有。1e-4（-80 dBFS）把上面那两个实测值分在两侧，余量很宽。
        #:
        #: **这道闸只回答「这个设备在不在出声」，不回答「它够不够灵敏」。** 2026-09-04 更正
        #: 过一次由此外推的错误结论：同一只阵列曾被据此判成「不该用」，而使用者实测它
        #: **能正常完成语音唤醒**。峰值分不清「轻的语音」和「没有语音」（见
        #: `core/audio/vad.py` 模块头），所以从一个低峰值只能推出「此刻没有大声音」，
        #: 推不出「这只麦克风不行」。判灵敏度要先让 VAD 确认有人在说话。
        self.on_input_silent = on_input_silent
        #: 每一块音频的峰值往哪送。``None`` = 不送。
        #:
        #: 唤醒球的振幅靠它 —— 在这之前球只在换状态时收到一个固定振幅，所以「在听」那一态
        #: 是个匀速的呼吸：球一直在动，但它动的不是你说的话。限流**不在这里**（在
        #: `core/desktop_bridge.py` 的 `set_level`）：音频回调要尽可能短，而「多久发一次」
        #: 是消费者的事。
        self.on_level: Any = None
        self.silent_peak = silent_peak
        self.silent_grace_s = silent_grace_s
        #: KWS 命中的出口，在声纹门**之前**。见 ``_authorise`` 里那段注释：一次被声纹
        #: 拒绝的唤醒和一次根本没命中的唤醒在用户眼里一样，而根因完全不同。
        self.on_kws_hit = on_kws_hit
        #: KWS 命中总次数（含被拒的）。和 ``on_kws_hit`` 分开：计数是给「就绪清单」用的，
        #: 回调是给日志用的，而一个没接回调的调用方仍然该能看到数字。
        self.kws_hits = 0
        #: 自适应输入增益（``core/audio/gain.AutoGain``）。``None`` = 不处理，原样透传 ——
        #: 测试与验收脚本要能拿到未经处理的电平。
        self.auto_gain = auto_gain
        #: 「唤醒被接受但没进聆听」的次数与最后一次的原因。**这是「命中之后没有后文」
        #: 唯一的读数** —— 缺 ASR 与缺 on_recognized 在用户那里长得一样。
        self.listen_refusals = 0
        self.last_listen_refusal = ""
        #: 同一件事的回调出口，让 runtime 把它写进运行日志。
        self.on_listen_refused: Any = None

        #: --- 聆听宽限期 ---------------------------------------------------
        #:
        #: **修的是「唤醒后不马上说话就再也不听了」。** 流式识别器的端点检测在一个字都没
        #: 解出来时也会报端点 —— `rule1_min_trailing_silence=2.4`，也就是静默 2.4 秒。
        #: 此前 `_recognize` 收到那一下就把聆听结束掉：`_listening=False`、流 reset、
        #: 转写是空的所以**不通知任何人**。后果有两层，而且都是静默的：
        #:
        #: 1. 状态机还停在 LISTENING（唤醒球一直显示「在听」），而采集已经回到 KWS 模式 ——
        #:    球在说谎；
        #: 2. 使用者停顿两秒后再说话，那些话只喂给 KWS，于是「它不听我了」。
        #:
        #: 现在：端点报空转写时，还在宽限期内就**换一条新的识别流继续听**（中途停顿因此
        #: 不致命），超过了才结束并调 ``on_listen_expired`` —— 那一步让状态退回 IDLE，
        #: 「它不听了」这件事因此可见。
        self.listen_grace_s = float(listen_grace_s)
        self.on_listen_expired: Any = None
        #: 宽限期内换过几次识别流 / 因超时结束过几次聆听。给诊断读。
        self.listen_restarts = 0
        self.listen_expiries = 0
        self._listen_started_at = 0.0
        #: 本次 start() 以来见过的最大绝对样本值。诊断与就绪清单读它。
        self.input_peak = 0.0
        #: 已收到的音频块数。乘 blocksize/sample_rate 就是流时长。
        self.input_blocks = 0
        #: 判定成立时置位；只报一次，不每块都喊。
        self.input_silent = False
        self._silent_reported = False

        #: --- 输出静音窗 ---------------------------------------------------
        #:
        #: **为什么需要它。** 唤醒命中之后有两件事同时发生：识别器开始听（``_start_listening``），
        #: 以及确认音从**扬声器**放出来（``VoiceRuntime._greet``）。麦克风听得见扬声器，
        #: 所以那 0.8–1.6 秒的「你说吧」会被采进识别器，而它一放完就是一段静音 ——
        #: 端点检测正好在这时触发。两个结局都是坏的：
        #:
        #: - 转写非空（比如就是「你说吧」）-> 拿确认音自己当请求跑了一整轮，
        #:   而 ``_listening`` 已经归零，使用者真正要说的话没有一个字被听见；
        #: - 转写为空 -> 「空转写不开启回合」，直接回 KWS 模式，同样没有后文。
        #:
        #: 表现就是使用者 2026-08-30 报的「**有几率**在唤醒后不能进行后续的对话」——
        #: 「有几率」正是因为它取决于人有没有在确认音放完之前开口。更讽刺的是出厂那四句
        #: 是**按「喂回 ASR 能不能识别回原文」挑出来的**（见 acks.py 那张表），也就是说
        #: 它们恰好是最容易劫持转写的那一批。
        #:
        #: 做法是在这段窗口里**整块丢弃**，不是喂静音：喂静音会让识别器内部的时间继续走，
        #: 端点照样可能触发；丢弃则让识别器停在原地等真正的人声。代价是人如果抢在确认音
        #: 上说话，那几个字会丢 —— 而它们本来也和确认音混在一起，转写不出什么。
        self._mute_until = 0.0
        #: 被静音窗丢掉的块数。给「就绪清单」和诊断读 —— 一个静音窗如果因为哪里算错了
        #: 一直不解除，症状会是「完全不响应」，那时这个数字是唯一的读数。
        self.muted_blocks = 0
        #: 半双工窗的截止时刻。扬声器在放回答的这段时间里转写与电平停掉，**唤醒判定继续
        #: 跑** —— 见 ``duck_for``。这是「朗读时能随时打断」的全部机制。
        self._duck_until = 0.0
        #: 半双工窗里没进转写的块数（**不是整块丢掉**：KWS 与环形缓冲照收）。
        self.ducked_blocks = 0
        #: 唤醒判定被按住到什么时候。见 ``hold_wake_for`` —— 控制台取样期间用。
        self._wake_held_until = 0.0
        self.wake_holds = 0
        #: 被 VAD 判成语音的块数。``speech_blocks / input_blocks`` 就是「这段时间里
        #: 有多少是人在说话」—— 一个长期为 0 的比例说明麦克风只收到了房间。
        self.speech_blocks = 0
        #: ``start(enroll_only=True)`` 置位：设备开着、缓冲照常填，但**唤醒永不判定**。
        #: 存在的理由见 ``start`` 的注释 —— 第一次注册的鸡生蛋问题。
        self.enroll_only = False
        #: 托盘的「暂停唤醒」。无限期，只有 ``resume_wake()`` 能解开 —— 见那两个方法。
        self.wake_paused = False

    # -- lifecycle helpers ----------------------------------------------------

    @staticmethod
    def _safe_call(obj: Any, method_name: str, *args: Any) -> None:
        """Call an optional teardown/reset method without blocking cleanup."""
        try:
            method = getattr(obj, method_name, None)
            if method is not None:
                method(*args)
        except Exception:
            # Teardown is best-effort. In particular, never let a native audio
            # error prevent the other resources from being reset.
            pass

    def _cleanup_resources(self) -> None:
        """Best-effort teardown used by both failed starts and ``stop``.

        Fields are detached before calling foreign/native code. That makes the
        operation idempotent even when a stream or provider raises, and prevents
        a later cleanup attempt from repeating the same side effect.
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            self._safe_call(stream, "stop")
            self._safe_call(stream, "close")

        asr_stream, self._asr_stream = self._asr_stream, None
        self._listening = False
        if asr_stream is not None:
            self._safe_call(self.asr_provider, "reset", asr_stream)

        self._inference_stream = None
        self._callback_faulted = False
        self._ring.clear()

        keyword_loaded, self._keyword_provider_loaded = self._keyword_provider_loaded, False
        if keyword_loaded:
            self._safe_call(self.keyword_provider, "close")

        asr_loaded, self._asr_provider_loaded = self._asr_provider_loaded, False
        if asr_loaded:
            self._safe_call(self.asr_provider, "close")

        verifier_loaded, self._verifier_loaded = self._verifier_loaded, False
        if verifier_loaded:
            self._safe_call(self.verifier, "close")

    def _reset_kws_stream(self) -> bool:
        """Replace a possibly poisoned KWS stream after a callback failure."""
        had_kws_state = self._inference_stream is not None or self._keyword_provider_loaded
        self._inference_stream = None
        if not had_kws_state:
            return True
        try:
            self._inference_stream = self.keyword_provider.create_stream()
        except Exception:
            # A callback cannot safely stop sounddevice from inside itself. Keep
            # the device object for ``stop()``, but stop processing future audio.
            self._callback_faulted = True
            return False
        self._callback_faulted = False
        return True

    def _recover_after_callback_error(self) -> None:
        """Return to a safe KWS-only state after an exception in the callback."""
        asr_stream, self._asr_stream = self._asr_stream, None
        self._listening = False
        if asr_stream is not None:
            self._safe_call(self.asr_provider, "reset", asr_stream)
        self._ring.clear()
        self._reset_kws_stream()

    def _record_callback_error(self, exc: Exception) -> None:
        self.callback_errors += 1
        # Exception messages can contain paths, user text, or provider details.
        # The callback surface retains only a non-sensitive type name.
        self.last_callback_error = type(exc).__name__

    # -- gate ----------------------------------------------------------------

    @property
    def gate_active(self) -> bool:
        """Whether a wake hit will actually be checked against a voiceprint."""
        return self.verifier is not None and self.require_verification

    def _check_gate_preconditions(self) -> None:
        """Refuse to start when the configured gate cannot possibly hold.

        缺 verifier、模型读不出来 —— 这两条一律抛。**没有一条路是「记个警告然后照样开麦」**，
        那才是 fail-closed 的实际含义。

        「一个人都没注册」是**唯一**的例外，而且它不是放宽：这种情况下走
        ``enroll_only`` —— 设备开着、缓冲照常填，但 ``wake_held`` 恒真，``_authorise``
        永远不会被调到，所以没有任何唤醒能被接受。真正不许绕过的断言是「唤醒不经校验不许
        通过」，那一条仍然成立。

        为什么要这个例外：此前它是一个死锁 —— 声纹门不许开麦，而控制台注册要从采集缓冲取
        音频，于是**第一次注册只能用命令行脚本**。使用者的要求是「希望在控制台能进行全部
        的设置，包括第一次录制声纹」，而一个必须先开终端才能用的产品配不上「成熟」这个词。
        """
        if not self.require_verification:
            return
        if self.verifier is None:
            raise ProviderUnavailable(
                "speaker verification is required but no verifier is attached; "
                "pass require_verification=False only if you accept that anyone "
                "can wake the platform"
            )
        self._verifier_loaded = True
        status = self.verifier.load()
        if not status.available:
            raise ProviderUnavailable(
                f"speaker verification is required but unusable: {status.details['reason']}"
            )
        if not self.verifier.speakers:
            # 死锁的出口，不是放宽：进 enroll_only 之后 `wake_held` 恒真，`_authorise`
            # 永远不会被调到，所以「唤醒不经校验不许通过」这条断言仍然成立。
            self.enroll_only = True

    def _report_verified(self, speaker: str | None) -> None:
        """Deliver the gate's identity verdict without letting it break the wake.

        A raising consumer is counted like any other callback fault. It is safe
        for it to raise on the *accept* call specifically, because the clear call
        already ran: the consumer is then left holding ``None``.
        """
        if self.on_verified is None:
            return
        try:
            self.on_verified(speaker)
        except Exception as exc:  # noqa: BLE001 - counted, never propagated
            self._record_callback_error(exc)

    def _note_kws_hit(self, keyword: str) -> None:
        """报一次 KWS 命中。计数总是加，回调可选且不许把音频线程带走。"""
        self.kws_hits += 1
        if self.on_kws_hit is None:
            return
        try:
            self.on_kws_hit(keyword)
        except Exception as exc:  # noqa: BLE001 - 和其他 sink 同一个姿态
            self._record_callback_error(exc)

    def _authorise(self, keyword: str) -> None:
        """Decide one wake hit, then drop the audio it was decided on."""
        # KWS 命中先报，再判门。**这两件事必须分开可见**：一次被声纹拒绝的唤醒和一次
        # 根本没命中的唤醒，在用户眼里长得一模一样（都是「没反应」），而根因完全不同 ——
        # 前者要调声纹/重注册，后者要调词表/阈值/麦克风。实机诊断里正是靠这一层的分离
        # 才看出「KWS 命中 16/16、声纹 0/16」。
        #
        # 报在最前面：下面每一条路（无门放行、verifier 抛异常、拒绝、接受）都已经算作
        # 「命中之后发生的事」。
        self._note_kws_hit(keyword)
        # Clear first, unconditionally. Every path below either leaves this
        # standing (no gate, error, rejection) or replaces it with a verified
        # name. There is no ordering in which a previous speaker's identity
        # survives into this attempt.
        self._report_verified(None)
        if not self.gate_active:
            # Escape hatch only: diagnose() reports this as a warning. No gate
            # means no verified identity, so nothing is reported here -- the
            # cleared value above is the honest answer.
            self.on_wake(keyword, None)
            self._start_listening()
            return
        window = self._ring.snapshot(self.verify_seconds)
        try:
            result = self.verifier.verify(window, sample_rate=self.sample_rate)
        except Exception as exc:  # a verifier fault is a rejection, never a pass
            if self.on_reject is not None:
                self.on_reject(keyword, f"verifier error: {type(exc).__name__}", 0.0)
            return
        finally:
            # The window has served its only purpose. Holding it longer widens
            # the biometric exposure for no benefit.
            self._ring.clear()
        if result.accepted:
            self._report_verified(result.speaker)
            self.on_wake(keyword, result.score)
            self._start_listening()
        elif self.on_reject is not None:
            self.on_reject(keyword, result.reason, result.score)

    def _start_listening(self) -> None:
        """Enter ASR mode after an accepted wake, so the follow-up speech is
        transcribed rather than fed to KWS.

        **不能声地返回。** 这两个前提缺任何一个，症状都是使用者 2026-08-29 报的那句
        「命中唤醒后没有后文，也不听我的后续指令」—— 球弹出来了、确认音也响了（那两件事
        走 ``on_wake``），但识别器从来没开，于是没有一个字被转写。此前这里是一句静默的
        ``return``，所以那个状态在任何地方都看不见。
        """
        if self.asr_provider is None:
            self._note_listen_refused("没有 ASR provider —— 唤醒之后不会转写任何东西")
            return
        if self.on_recognized is None:
            self._note_listen_refused("没有接 on_recognized —— 转写出来也没人接")
            return
        self._asr_stream = self.asr_provider.create_stream()
        self._listening = True
        self._listen_started_at = time.monotonic()

    def _note_listen_refused(self, reason: str) -> None:
        """记下「唤醒被接受了但没进聆听」。计数总是加，回调可选。"""
        self.listen_refusals += 1
        self.last_listen_refusal = reason
        if self.on_listen_refused is None:
            return
        try:
            self.on_listen_refused(reason)
        except Exception as exc:  # noqa: BLE001 - 和其他 sink 同一个姿态
            self._record_callback_error(exc)

    def _recognize(self, samples: Any) -> None:
        """Feed the recognizer; on an endpoint, deliver the final text and
        return to KWS mode.

        **空转写不等于「不听了」。** 端点检测在一个字都没解出来时也会报端点（静默 2.4 秒），
        而此前那一下会直接结束聆听且不通知任何人 —— 见 ``listen_grace_s`` 那段注释。
        现在空转写在宽限期内只是换一条新的识别流：人想两秒再开口是正常的。
        """
        if self._asr_stream is None:
            self._listening = False
            return
        result = self.asr_provider.feed(self._asr_stream, samples, self.sample_rate)
        if not result.is_endpoint:
            return
        asr_stream = self._asr_stream
        text = self.asr_provider.finalize(asr_stream)
        if not text.strip():
            self._restart_or_expire(asr_stream)
            return
        # Detach first so a reset/callback failure cannot trigger a second reset
        # from the outer recovery path.
        self._listening = False
        self._asr_stream = None
        self.asr_provider.reset(asr_stream)
        self.on_recognized(text.strip())

    def _restart_or_expire(self, asr_stream: Any) -> None:
        """端点到了但一个字都没有：还在宽限期内就继续听，否则结束聆听并报出来。"""
        self.asr_provider.reset(asr_stream)
        waited = time.monotonic() - self._listen_started_at
        if waited < self.listen_grace_s:
            # 换一条新的流而不是复用：reset 之后的流能再用，但换一条让「这一段静默不算」
            # 这件事在状态上干净，也避免任何残留的解码状态跨过这次停顿。
            self._asr_stream = self.asr_provider.create_stream()
            self.listen_restarts += 1
            return
        self._listening = False
        self._asr_stream = None
        self.listen_expiries += 1
        if self.on_listen_expired is None:
            return
        try:
            self.on_listen_expired(round(waited, 1))
        except Exception as exc:  # noqa: BLE001 - 和其他 sink 同一个姿态
            self._record_callback_error(exc)

    # -- 注册用的音频（和校验同一份缓冲）--------------------------------------

    @property
    def listening(self) -> bool:
        """现在是不是在「唤醒之后的聆听」阶段。

        公开它是因为控制台要据此拒绝取样：聆听期间音频**全部喂给识别器、一个样本都不进
        环形缓冲**，那时取快照只会拿到一段空的。
        """
        return bool(self._listening)

    def hold_wake_for(self, seconds: float) -> None:
        """这段时间内不判唤醒词，但**照常写环形缓冲**。

        存在的理由是 2026-08-31 实机报的「试一句经常 0 分」。控制台取样时会让人说话，而
        页面上提示说的正是唤醒词 —— 于是 KWS 真的命中：`_authorise` 走完之后 `finally`
        里 `_ring.clear()` 把刚录的清了，紧接着 `_start_listening()` 把模式切成聆听，
        之后的块**不再进环形缓冲**。取样结束时快照几乎是空的，质量门判「太轻」，
        分数 0。顺带还白弹一次球、白播一次确认音、白开一次聆听。

        所以取样期间要把唤醒这条路**按住**：不喂 KWS 就不会命中，缓冲照常填。
        这不放宽任何安全边界 —— 它让唤醒**更难**发生，而且只在本机已鉴权的取样窗口内。
        """
        try:
            span = float(seconds)
        except (TypeError, ValueError):
            return
        self._wake_held_until = time.monotonic() + span if span > 0 else 0.0

    def release_wake(self) -> None:
        """立刻恢复唤醒判定。"""
        self._wake_held_until = 0.0

    def arm_after_enrollment(self) -> bool:
        """注册完之后重判一次 ``enroll_only``。解开了返回 ``True``。

        **不重判就等于骗人。** ``enroll_only`` 只在 ``start()`` 里判一次，而控制台注册
        是在麦克风已经跑起来之后发生的 —— 2026-09-01 实机：使用者在页面上注册成功，
        页面写着「注册完就会响应唤醒词」，可 ``wake_held`` 仍然恒真，喊什么都没有反应，
        必须重启才行。页面上那句话是这个方法的存在理由。

        **只从「按住」走向「正常」，反向不做。** 关掉唤醒的判定权仍然只属于
        ``_check_gate_preconditions``：这里复用它的那一条前提（有人注册了吗），
        没有人注册就原样按住 —— 所以「唤醒不经校验不许通过」仍然成立。
        """
        if not self.enroll_only:
            return False
        if self.verifier is None or not getattr(self.verifier, "speakers", ()):
            return False
        self.enroll_only = False
        return True

    @property
    def wake_held(self) -> bool:
        # ``enroll_only`` 是永久的按住：那一路开麦只为了录注册样本。
        # ``wake_paused`` 是托盘按下的，只有托盘（或调用方）能解开。
        return (
            self.enroll_only
            or self.wake_paused
            or time.monotonic() < self._wake_held_until
        )

    def pause_wake(self) -> bool:
        """无限期按住唤醒判定（托盘的「暂停唤醒」）。返回是否发生了变化。

        和 ``hold_wake_for`` 分开是因为**时长的语义不同**：那一个是「取样期间别判词」，
        有个自然的结束时刻；这一个是用户明确说「先别听我说话」，只有用户能解开。用一个
        很大的秒数去冒充无限期会在某个下午突然过期，而那时没人会记得为什么。

        麦克风**不关**：关掉设备再重开要重新走 PortAudio 的初始化，而那条路会失败
        （设备被别的进程抢走、独占模式），于是「恢复」变成一个可能失败的动作。缓冲照常
        填、电平照常观测，只是不判词 —— 和注册模式同一个做法。
        """
        if self.wake_paused:
            return False
        self.wake_paused = True
        return True

    def resume_wake(self) -> bool:
        """解开「暂停唤醒」。

        **只解开自己按下的那一道。** ``enroll_only`` 与 ``hold_wake_for`` 的按压不受影响 ——
        否则托盘上点一下「恢复唤醒」就能绕过注册模式，而那一路开麦只为了录样本。
        """
        if not self.wake_paused:
            return False
        self.wake_paused = False
        return True

    def begin_listening(self, reason: str = "manual") -> bool:
        """不经唤醒词直接进聆听（托盘的「主动唤醒」）。开起来了返回 ``True``。

        **声纹门没有被绕过，而是没有输入可判。** 托盘点击发生在「还没有人说话」的那一刻，
        所以不存在一段音频可以拿去比对 —— 这里明确把已验证说话人清成 ``None``，于是
        ``shell.run`` 这类要求身份的工具照旧被拒。绕过的是唤醒词，不是那道门。

        暂停期间不开：点了「暂停唤醒」还能从同一个菜单唤醒它，那个开关就不是开关。
        已经在听时也不重开 —— 那会丢掉当前这条识别流里已经解出来的字。
        """
        if self._listening:
            return False
        if self.wake_paused or self.enroll_only:
            self._note_listen_refused(f"唤醒被按住（{reason}）—— 先恢复唤醒")
            return False
        self._report_verified(None)
        self._start_listening()
        return self._listening

    def resume_listening(self, reason: str = "follow-up") -> bool:
        """回答说完之后再开一次聆听 —— **连续对话**。开起来了返回 ``True``。

        和 ``begin_listening`` 只差一件事，而那件事是全部的重点：**这里不清已验证说话人。**

        - 托盘的主动唤醒发生在「还没有人说话」的那一刻，没有音频可比对，所以那条路必须
          把身份清成 ``None``。
        - 这一条发生在**同一轮对话刚说完**的那一刻：几秒之前才有一次真的声纹通过，
          而这个窗口是那次通过的延续。清掉它会让「你好小沃，帮我看看 X」→「那再跑一下测试」
          里的第二句突然没有身份 —— 一个在对话中途静默失去权限的助手比没有连续对话更糟。
        - 代价说清楚：这个窗口内**别人说话也会被当成已验证的那个人**。所以它必须短
          （由 ``listen_grace_s`` 收口，默认 8 秒，没人说话就自己结束），而且它只在一次
          成功的唤醒之后才存在 —— 它不能自己延长自己（每次回答之后重新开一个新窗口是
          调用方的决定，见 ``vox_plugin/runtime.py`` 的 ``_follow_up``）。

        暂停唤醒 / 注册模式下不开：那两个状态的语义是「现在不要听我说话」。
        """
        if self._listening:
            return False
        if self.wake_paused or self.enroll_only:
            self._note_listen_refused(f"唤醒被按住（{reason}）—— 先恢复唤醒")
            return False
        self._start_listening()
        return self._listening

    def has_speech(self, samples: Any) -> bool:
        """这一段音频里有没有人在说话。没接 VAD 时**放行**（返回 True）。

        控制台的取样 / 注册 / 试一句据此拒绝。用 VAD 而不是一条峰值线，是因为峰值分不清
        「轻的语音」和「放大后的底噪」—— 实测底噪 ×10 判 False，而真人声缩到峰值 0.01
        仍判 True。
        """
        gate = self.speech_gate
        checker = getattr(gate, "has_speech", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(samples))
        except Exception:  # noqa: BLE001 - 判不了就放行，它不是安全边界
            return True

    #: 环形缓冲的容量（秒）。调用方靠它知道最多能要多长。
    @property
    def buffer_seconds(self) -> float:
        return float(self._ring.seconds)

    def recent_audio(self, seconds: float) -> Any:
        """最近 ``seconds`` 秒输入音频的**副本**。

        存在的理由是**注册和校验必须同信道**。此前控制台的注册走浏览器 `getUserMedia`：
        那是**浏览器认为的默认设备**（不是 ``device``），带浏览器自己的 AGC / 降噪 /
        回声消除和采样率，而校验读的是这里这条流。两条链路各自都「录成功了」，比出来的
        相似度却是在比链路而不是比人。

        公开这个方法而不是让调用方去摸 ``_ring``：环形缓冲的所有权在这个类，
        「音频永不落盘、永不出进程」那条红线也守在这个类。

        快照是拷贝，可以在别的线程上取 —— `AudioRingBuffer.snapshot` 就是为此写的。
        """
        return self._ring.snapshot(seconds)

    def forget_recent_audio(self) -> None:
        """丢掉缓冲里的音频。注册前调用一次，免得把上一段的尾巴算进这一段。"""
        self._ring.clear()

    # -- 输出静音窗 -----------------------------------------------------------

    @property
    def muted(self) -> bool:
        """现在是否在静音窗里。"""
        return time.monotonic() < self._mute_until

    def mute_for(self, seconds: float) -> None:
        """从现在起丢弃 ``seconds`` 秒的输入。

        语义是**赋值**而不是「取更大的那个」，这是刻意的：调用方的用法是「播放前压一个
        够长的上限，播放（阻塞）结束后再压一个短尾巴」，第二次调用必须能把窗口收回来，
        否则每次唤醒都会白聋掉上限那么久。

        没有锁：一次浮点赋值在 GIL 下是原子的，而读侧（音频回调）读到旧值或新值都自洽。
        为此存的是**绝对截止时刻**而不是「剩余块数」—— 后者需要读改写，那才真的需要锁。
        """
        try:
            span = float(seconds)
        except (TypeError, ValueError):
            return
        self._mute_until = time.monotonic() + span if span > 0 else 0.0

    def unmute(self) -> None:
        """立刻解除静音窗。"""
        self._mute_until = 0.0

    # -- 半双工窗（扬声器在响，但唤醒词仍然要听得见）---------------------------

    @property
    def ducking(self) -> bool:
        """现在是否在**半双工窗**里 —— 扬声器在放回答，而唤醒判定仍然开着。"""
        return time.monotonic() < self._duck_until

    def duck_for(self, seconds: float) -> None:
        """扬声器要响 ``seconds`` 秒：**丢转写和电平，但继续听唤醒词。**

        和 ``mute_for`` 的区别是全部的重点。静音窗在回调最前面 ``return``，于是播放期间
        KWS 一块音频都收不到 —— 那正是「朗读时必须等它读完或者重新喊唤醒词」的成因：
        想打断的那句话落在一个聋掉的麦克风上。

        半双工窗只关三样：

        * **转写**（``_recognize``）—— 开着的话助手会把自己的回答转写成下一句请求；
        * **电平观测** —— 拿我们自己放出去的声音证明麦克风活着是假证据；
        * **自适应增益的适应** —— 让它跟着扬声器的包络走，人一开口就偏了。

        唤醒判定和环形缓冲**继续跑**。缓冲必须继续写，因为打断也要过声纹门，而声纹看的是
        命中之前那 3 秒 —— 不写缓冲就等于「能听见打断但永远验不过」。

        代价说清楚：缓冲里此刻是「人声 + 扬声器串音」的混合。**戴耳机时几乎没有串音**；
        用音箱且音量大时相似度会掉，表现是打断偶尔要说两次。反过来，助手自己的 TTS 声音
        触发 KWS 时会被声纹门拒掉（那不是注册的那个人），所以自激**不会**变成误唤醒 ——
        半双工窗能开着正是因为门在后面。
        """
        try:
            span = float(seconds)
        except (TypeError, ValueError):
            return
        self._duck_until = time.monotonic() + span if span > 0 else 0.0

    def unduck(self) -> None:
        """立刻收掉半双工窗。播放提前结束（被打断）时调用。"""
        self._duck_until = 0.0

    # -- capture -------------------------------------------------------------

    @staticmethod
    def _block_peak(samples: Any) -> float:
        """一块音频的峰值绝对值。对 numpy 走向量化，对别的退回 Python。

        不在模块顶层 import numpy：这个模块此前不依赖它，而 sounddevice 交来的本来就是
        ndarray。一个喂 list 的测试替身仍然要能跑，所以两条路都留。
        """
        try:
            return float(abs(samples).max())
        except (TypeError, ValueError, AttributeError):
            try:
                return float(max(abs(float(value)) for value in samples))
            except (TypeError, ValueError):
                return 0.0

    def _watch_input_level(self, samples: Any) -> None:
        """记峰值，并在宽限期后判一次「这个设备没在出声」。

        只判一次：一个死设备每 100 ms 喊一遍毫无信息量，而回调线程上的重复调用是真实
        成本。判定成立后 ``input_silent`` 一直为真，直到下一次 ``start()``。
        """
        self.input_blocks += 1
        peak = self._block_peak(samples)
        if peak > self.input_peak:
            self.input_peak = peak
        # 电平送出去（球的振幅）。**吞掉异常**：一个坏掉的可视化不能带走音频线程。
        if self.on_level is not None:
            try:
                self.on_level(peak)
            except Exception:  # noqa: BLE001
                pass
        if self._silent_reported or self.input_peak > self.silent_peak:
            return
        elapsed = self.input_blocks * self.blocksize / max(1, self.sample_rate)
        if elapsed < self.silent_grace_s:
            return
        self._silent_reported = True
        self.input_silent = True
        if self.on_input_silent is None:
            return
        try:
            self.on_input_silent(
                {
                    "device": self.device if self.device is not None else "(系统默认)",
                    "peak": self.input_peak,
                    "seconds": round(elapsed, 1),
                    "threshold": self.silent_peak,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 回调不能把音频线程带走
            self._record_callback_error(exc)

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if self._callback_faulted or status:
            # Capture status is intentionally left to the caller's logger. A
            # failed recovery keeps the stream alive only for best-effort stop.
            return
        try:
            samples = indata[:, 0]
            # 静音窗:自己的扬声器在响,这一块整个丢掉。**放在最前面**,连电平观测也不做 ——
            # 那一层是「死麦克风检测」,拿我们自己放出去的声音去证明麦克风活着是假证据。
            if self.muted:
                self.muted_blocks += 1
                return
            # 半双工窗：扬声器在放回答。**只关转写与电平，唤醒判定继续跑** —— 这一行是
            # 「朗读时能随时打断」和「必须等它读完」的分界。见 duck_for 的文档串。
            ducking = self.ducking
            if ducking:
                self.ducked_blocks += 1
            # 电平先看,再分流。放在 _listening 判断**之前**是因为一个死设备在聆听阶段
            # 同样是死的,而那时的症状是「唤醒了但转写永远是空」。
            #
            # 半双工窗里不看：拿我们自己放出去的声音去证明麦克风活着是假证据。
            if not ducking:
                self._watch_input_level(samples)
            # 自适应增益紧跟在电平观测之后:观测要看**设备真实**电平（否则「全零」判定会被
            # 增益骗过去）。
            #
            # **2026-08-31：增益只喂 KWS 与 ASR，环形缓冲存原始音频。** 此前两边都吃加过
            # 增益的样本，那是一条会伪造现实的路：使用者的设备原始峰值是 0.0587（五分钟
            # 的最大值），底噪高于 AutoGain 的 floor_peak(0.004)，于是增益一路爬到 ~10 倍，
            # 缓冲里的「静音」变成 rms 0.21 / peak 0.53 —— 看上去是一段健康的语音。后果是
            # 三层同时失效：
            #
            #   1. 声纹的质量门（min_rms=0.002）跑在增益**之后**，于是永远不可能触发；
            #   2. 从缓冲注册的档案录到的是**放大后的房间底噪**，不是人声；
            #   3. 拿新的一段底噪去比那个档案，余弦 0.979「通过」—— 门变成了 fail-open。
            #
            # 声纹路径本来就在 `embed()` 里按峰值归一化，所以增益对它**一点好处都没有**，
            # 只有「让质量门失灵」这一个作用。KWS/ASR 需要接近训练电平，所以增益留给它们。
            voiced = samples
            # VAD 先判，因为增益要**只在语音上适应**。见 core/audio/vad.py 的模块头：
            # 一个跟峰值的 AGC 在原理上分不清「轻的语音」和「没有语音」，而那正是
            # 2026-08-31 把一道 fail-closed 的门变成 fail-open 的那件事。
            speaking = self.speech_gate(samples) if self.speech_gate is not None else None
            if speaking:
                self.speech_blocks += 1
            if self.auto_gain is not None:
                # 半双工窗里不让增益**适应**（`is_speech=False`）：扬声器的包络会把它带跑，
                # 人一开口时增益已经偏了。仍然按当前增益放大，因为 KWS 要接近训练电平。
                voiced = self.auto_gain.apply(samples, is_speech=False if ducking else speaking)
            if self._listening:
                # 扬声器在响的时候不转写 —— 否则助手把自己的回答当成下一句请求。
                # 这是半双工窗关掉的那三样里唯一一条会产生**错误内容**的。
                if ducking:
                    return
                self._recognize(voiced)
                return
            self._ring.write(samples)
            # **不用 VAD 去闸 KWS。** 它是流式解码器，喂一条被切碎的流可能反而降低命中率，
            # 而命中率正是要保住的东西。VAD 在这里的作用是驱动增益 + 回答「这一段有语音吗」。
            # 唤醒判定被按住时只填缓冲、不判词。控制台取样期间走这条 —— 见 hold_wake_for：
            # 取样提示说的就是唤醒词，真命中会把刚录的缓冲清掉并切进聆听模式。
            if self.wake_held:
                self.wake_holds += 1
                return
            for keyword, _kws_score in self.keyword_provider.feed(
                self._inference_stream, voiced, self.sample_rate
            ):
                self._authorise(keyword)
        except Exception as exc:
            self._record_callback_error(exc)
            self._recover_after_callback_error()

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ImportError as exc:
            raise ProviderUnavailable("sounddevice is not installed") from exc
        try:
            # Gate first: a refused gate must not leave a device open behind it.
            # enroll_only 每次 start 重新判定 —— 注册完之后再开麦就该是正常模式。
            self.enroll_only = False
            self._check_gate_preconditions()
            self._keyword_provider_loaded = True
            status = self.keyword_provider.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])
            self._inference_stream = self.keyword_provider.create_stream()
            if self.asr_provider is not None:
                self._asr_provider_loaded = True
                asr_status = self.asr_provider.load()
                if not asr_status.available:
                    raise ProviderUnavailable(asr_status.details["reason"])
            self._ring.clear()
            self._callback_faulted = False
            # 电平统计按「本次会话」计:换设备重开之后,上一次的峰值不该继续背在身上。
            self.input_peak = 0.0
            self.input_blocks = 0
            self.input_silent = False
            self._silent_reported = False
            self.speech_blocks = 0
            # 静音窗同理:上一次会话结束时还挂着的窗口不该让新会话开局就聋着。
            self._mute_until = 0.0
            self.muted_blocks = 0
            self._wake_held_until = 0.0
            self.wake_holds = 0
            self._stream = sounddevice.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="float32",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception:
            # Includes provider/model errors, InputStream construction errors,
            # and InputStream.start() failures. All partial resources are reset so
            # the next call can retry from a clean transaction boundary.
            self._cleanup_resources()
            raise

    def stop(self) -> None:
        self._cleanup_resources()
