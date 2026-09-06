"""流式识别走 WebSocket —— **说完到文本 2734 ms → 93 ms。**

## 为什么这条路快 2.6 秒

整段那条路（`asr_cloud.py`）是「说完 → 把整段 POST 上去 → 等」。流式路上音频**边说边传**，
所以说完之后只剩最后一块的处理时间。2026-09-05 实测（同一段真录音、按 100 ms 真实节奏喂）：

| 模型 | 说完 → 最终文本 | 「沃」写对了吗 |
|---|---|---|
| **`fun-asr-realtime`** | **93 ms** | ✓ 「你好，小沃。」 |
| `paraformer-realtime-v1` | 108 ms | ✗ 「你好，小**吴**」 |
| `paraformer-realtime-v2` | 359 ms | ✗ 「你好小**吴**」 |

准确度这一列必须一起看：写不出「沃」的模型再快也是净损失 —— 那是 2026-09-03 把识别搬上云的
全部理由（本机 14M 模型的字表里没有这个字）。所以默认是 `fun-asr-realtime`，两项都赢。

## 端点仍然由本机 VAD 判，这是刻意的

服务端自己会在句间停顿处切句（`sentence_end`），用它当「一轮结束」看起来更省事。不用它的
理由是那个阈值不由我们定：这位说话人的短语间停顿实测 **1.0–1.1 秒**，而
`docs/research/prototype-results.md` 里那张表证明过 —— 阈值降到 1.0 就会把一句话切成两半，
后半句落进下一轮，而使用者看到的是「它没听全」。

所以这一层的分工是：**服务端负责出字，我们负责判「说完了」**。`silence_s` 与那条路的
`rule2_min_trailing_silence` 是同一个数，改动它照样受同一条测试保护。

## `feed()` 永不阻塞

和整段那条路同一条不变式。端点到达时只发 `finish-task`，最终文本由后台读线程收，下一次
`feed()` 取走 —— 延迟因此多一个块（100 ms），换来的是音频回调线程一次都不会等网络。
在回调里阻塞 300 ms 的后果是丢帧，而丢帧的症状是「识别器听错」，最难查的那一类。

## 出网边界

和整段那条路一样：只有「被接受的唤醒之后说的那一句」出网，唤醒词那一块不进这里（capture
的两段模式保证）。音频以裸 PCM 走 WebSocket 二进制帧，不写盘、不留缓存、不传 URL。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.audio.asr import AsrResult
from core.audio.base import ProviderStatus, ProviderUnavailable
from core.audio.vad import SileroSpeechGate
from core.outbound import API_USER_AGENT
from core.ws import OP_TEXT, WebSocketClient, WebSocketError, connect

ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
SAMPLE_RATE = 16000
DEFAULT_MODEL = "fun-asr-realtime"
DEFAULT_KEY_ENV = "VOX_ASR_KEY"


class WsAsrError(RuntimeError):
    """协议层的失败。带上服务端的 `error_code` —— `ModelNotFound` 和额度耗尽是两件事。"""


@dataclass
class _WsStream:
    """一轮聆听 = 一条 WebSocket + 一个读线程 + 一个 VAD。"""

    client: WebSocketClient
    task_id: str
    gate: Any = None
    reader: Any = None
    lock: Any = field(default_factory=threading.Lock, repr=False)
    #: 服务端认为已经说完的句子，按到达顺序。
    sentences: list[str] = field(default_factory=list)
    #: 当前还在变的那一句。**最终文本要带上它** —— 端点由我们判，可能落在服务端认为
    #: 句子还没结束的时刻，那时丢掉 partial 就是丢掉最后半句话。
    partial: str = ""
    finished: bool = False
    error: str = ""
    committed: bool = False
    #: commit 的时刻。**「说完 → 最终文本多少毫秒」这个数只有在这一层量得到**，
    #: 而它是这条路存在的全部理由（整段那条是 2734 ms，这条实测 93 ms）。
    commit_at: float = 0.0
    frames: int = 0
    speech_frames: int = 0
    silence_frames: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def text(self) -> str:
        with self.lock:
            parts = [part for part in (*self.sentences, self.partial) if part.strip()]
        return "".join(parts).strip()


@dataclass
class DashScopeWsAsrProvider:
    """百炼流式识别，摆成和 `SherpaStreamingAsrProvider` / `DashScopeAsrProvider` 一样的形状。"""

    model: str = DEFAULT_MODEL
    key_env: str = DEFAULT_KEY_ENV
    endpoint: str = ENDPOINT
    #: 尾部静音判「说完了」。**和整段那条路同一个数**，理由见模块头。
    silence_s: float = 0.8
    #: 一段最长多久。到了就 commit，不等静音。
    max_utterance_s: float = 30.0
    #: 至少要有这么多语音才算一句话（挡掉一次咳嗽）。
    min_utterance_s: float = 0.35
    timeout_s: float = 30.0
    vad_model: str = ""
    #: 注入点，给测试用。默认是真的 WebSocket。
    connector: Any = None
    requests: int = 0
    failures: int = 0
    last_error: str = ""
    last_commit_ms: int = 0

    @property
    def available(self) -> bool:
        return bool(os.getenv(self.key_env, "").strip())

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, self._label(), {"reason": f"{self.key_env} 没有值", "engine": "dashscope-ws"}
            )
        return ProviderStatus(True, self._label(), self.describe())

    def _label(self) -> str:
        return f"{self.model} @ {self.endpoint}"

    def describe(self) -> dict[str, Any]:
        return {
            "engine": "dashscope-ws",
            "model": self.model,
            "endpoint": self.endpoint,
            "key_env": self.key_env,
            "available": self.available,
            "requests": self.requests,
            "failures": self.failures,
            "silence_s": self.silence_s,
            "last_commit_ms": self.last_commit_ms,
            "error": self.last_error,
        }

    # -- 一轮 ----------------------------------------------------------------

    def create_stream(self) -> _WsStream:
        """开一条流。**握手在这里发生，而这一刻是免费的** —— 它排在唤醒命中之后、
        应答音正在播的那 0.8–1.6 秒里，使用者还没开口。实测握手 172–266 ms。
        """
        key = os.getenv(self.key_env, "").strip()
        if not key:
            raise ProviderUnavailable(f"{self.key_env} 没有值")
        opener = self.connector or connect
        client = opener(
            self.endpoint,
            headers={"Authorization": f"bearer {key}", "User-Agent": API_USER_AGENT},
            timeout_s=self.timeout_s,
        )
        task_id = uuid.uuid4().hex
        client.send_text(
            json.dumps(
                {
                    "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio",
                        "task": "asr",
                        "function": "recognition",
                        "model": self.model,
                        "parameters": {"format": "pcm", "sample_rate": SAMPLE_RATE},
                        "input": {},
                    },
                },
                ensure_ascii=False,
            )
        )
        gate = None
        if self.vad_model:
            gate = SileroSpeechGate(
                self.vad_model,
                sample_rate=SAMPLE_RATE,
                # 句中换气 0.2–0.5 s 很常见，min_silence 必须比它长否则一句被切成两句。
                min_silence_duration=max(0.30, self.silence_s * 0.5),
            )
        stream = _WsStream(client=client, task_id=task_id, gate=gate)
        stream.reader = threading.Thread(target=self._read, args=(stream,), daemon=True)
        stream.reader.start()
        self.requests += 1
        return stream

    def _read(self, stream: _WsStream) -> None:
        """后台读事件。**这个线程只写 stream 上那几个字段，从不碰 provider 的计数器** ——
        计数器由 `feed`/`finalize` 在调用方线程上动，两边分开就不需要第二把锁。"""
        while True:
            try:
                message = stream.client.recv()
            except WebSocketError as exc:
                with stream.lock:
                    stream.error = str(exc)
                    stream.finished = True
                return
            if message is None:
                with stream.lock:
                    stream.finished = True
                return
            opcode, payload = message
            if opcode != OP_TEXT:
                continue
            try:
                event = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            header = event.get("header") or {}
            name = str(header.get("event", ""))
            if name == "task-failed":
                with stream.lock:
                    stream.error = (
                        f"{header.get('error_code', '未知')}: {header.get('error_message', '')}"
                    )
                    stream.finished = True
                return
            if name == "task-finished":
                with stream.lock:
                    stream.finished = True
                return
            if name != "result-generated":
                continue
            sentence = ((event.get("payload") or {}).get("output") or {}).get("sentence") or {}
            text = str(sentence.get("text", "") or "")
            if not text:
                continue
            done = bool(sentence.get("sentence_end") or sentence.get("end_time"))
            with stream.lock:
                if done:
                    stream.sentences.append(text)
                    stream.partial = ""
                else:
                    stream.partial = text

    # -- 喂音频 --------------------------------------------------------------

    def feed(self, stream: Any, samples: Any, sample_rate: int = SAMPLE_RATE) -> AsrResult:
        """收一块音频，发出去，顺带用本机 VAD 看「说完了没」。**永不阻塞。**

        `sample_rate` 收下但不重采样：capture 的流是 16 kHz 开的，一个不是 16 k 的输入是
        配置错误而不是这一层该修补的事。
        """
        import numpy as np

        block = np.asarray(samples, dtype="float32").reshape(-1)
        if not stream.committed:
            pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            try:
                stream.client.send_binary(pcm)
            except WebSocketError as exc:
                # 发不出去就当这一轮到此为止：已经收到的文本仍然算，而**报出来**
                # —— 「静音」和「网断了」在使用者那一侧同形。
                with stream.lock:
                    stream.error = str(exc)
                    stream.finished = True
                    stream.committed = True
                self.failures += 1
                self.last_error = str(exc)
                return self._settle(stream)
        stream.frames += int(block.size)
        if self._speaking(stream, block):
            stream.speech_frames += int(block.size)
            stream.silence_frames = 0
        else:
            stream.silence_frames += int(block.size)

        if stream.committed:
            return self._settle(stream)

        speech = stream.speech_frames / float(SAMPLE_RATE)
        silence = stream.silence_frames / float(SAMPLE_RATE)
        seconds = stream.frames / float(SAMPLE_RATE)
        ended = speech >= self.min_utterance_s and silence >= self.silence_s
        if not (ended or seconds >= self.max_utterance_s):
            return AsrResult("", False)
        if speech < self.min_utterance_s:
            # 到了长度上限但一句语音都没有：不 commit，让它继续听。清计数免得立刻再撞上限。
            stream.frames = 0
            stream.silence_frames = 0
            return AsrResult("", False)
        self._commit(stream)
        return self._settle(stream)

    def _commit(self, stream: Any) -> None:
        """告诉服务端「说完了」。最终文本由读线程收，下一次 `feed` 取走。"""
        stream.committed = True
        stream.commit_at = time.monotonic()
        try:
            stream.client.send_text(
                json.dumps(
                    {
                        "header": {
                            "action": "finish-task",
                            "task_id": stream.task_id,
                            "streaming": "duplex",
                        },
                        "payload": {"input": {}},
                    },
                    ensure_ascii=False,
                )
            )
        except WebSocketError as exc:
            with stream.lock:
                stream.error = str(exc)
                stream.finished = True
            self.failures += 1
            self.last_error = str(exc)

    def _settle(self, stream: Any) -> AsrResult:
        """commit 之后每一块都问一次：最终文本到了吗。**不等** —— 没到就返回空。"""
        with stream.lock:
            finished = stream.finished
            error = stream.error
        if not finished:
            return AsrResult("", False)
        if error and not stream.text():
            self.last_error = error
            return AsrResult("", True)
        started = float(getattr(stream, "commit_at", 0.0) or 0.0)
        if started:
            self.last_commit_ms = int((time.monotonic() - started) * 1000)
        return AsrResult(stream.text(), True)

    def _speaking(self, stream: Any, block: Any) -> bool:
        """这一块里有人说话吗。VAD 起不来时**恒真** —— 那时端点只由静音长度决定，
        而静音长度在没有 VAD 的情况下永远不增长，于是退化成「说满 max_utterance_s 才算完」。
        比「一句都不算完」好，而且 `describe()` 会说 VAD 没起来。"""
        gate = stream.gate
        if gate is None:
            return True
        try:
            return bool(gate(block))
        except Exception:  # noqa: BLE001 - 和 SileroSpeechGate 自己的姿态一致：坏了就放行
            return True

    # -- 收尾 ----------------------------------------------------------------

    def finalize(self, stream: Any) -> str:
        """冲洗出最终文本。**这里可以等** —— 调用方已经决定这一轮结束了。

        等的上限刻意很短（1 秒）：实测说完到最终文本是 93–359 ms，而一个等到超时的
        `finalize` 会把「网络慢」变成「它没反应」。等不到就把手上有的交出去。
        """
        if not stream.committed:
            self._commit(stream)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with stream.lock:
                if stream.finished:
                    break
            time.sleep(0.02)
        started = float(getattr(stream, "commit_at", 0.0) or 0.0)
        if started:
            self.last_commit_ms = int((time.monotonic() - started) * 1000)
        with stream.lock:
            if stream.error:
                self.last_error = stream.error
        text = stream.text()
        self._shut(stream)
        return text

    def reset(self, stream: Any) -> None:
        """这一轮不要了。**关掉连接而不是复用** —— 一条已经 commit 的流在协议上结束了，
        而复用它需要一个「重开任务」的动作，那是又一个会错的地方。"""
        self._shut(stream)

    @staticmethod
    def _shut(stream: Any) -> None:
        try:
            stream.client.close()
        except Exception:  # noqa: BLE001 - 收尾路径上抛异常会盖掉真正的原因
            pass

    def close(self) -> None:
        """provider 自己没有常驻资源：连接是一轮一条，由 `finalize` / `reset` 收。"""

    def take_error(self) -> str:
        error, self.last_error = self.last_error, ""
        return error


__all__ = ["DEFAULT_KEY_ENV", "DEFAULT_MODEL", "ENDPOINT", "DashScopeWsAsrProvider", "WsAsrError"]

