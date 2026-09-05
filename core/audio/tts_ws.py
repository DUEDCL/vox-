"""流式合成走 WebSocket —— **同一个模型、同一个音色，整段快 2.6 秒。**

## 为什么值得多一条路

2026-09-05 实测同一句「好，开好了。」、同一个 `qwen-audio-3.0-tts-plus` + `longanhuan_v3.6`：

| 传输 | 第一块音频 | 整段到手 |
|---|---|---|
| HTTP SSE（`tts_cloud.py`） | 2015 ms | **3578 ms** |
| **WebSocket（这里）** | **702 ms** | **936 ms** |

差的不是合成速度，是 **HTTP 层的固定开销**：建连 + TLS 握手 + 服务端把 SSE 攒到一定量才
下发。2026-09-01 记的那句「SSE 首块到达时间与句子长度基本无关（2.3–2.6 s）」现在有了解释 ——
那 2 秒从来不在合成里。

顺带一个协议差异：**`instruction` 在 WS 上放 `parameters` 里，HTTP 上放 `input` 里。** 它
真的生效 —— 带上「语速稍慢」之后同一句话的音频从 1.33 s 变成 1.49 s。放错位置不会报错，
只会让使用者配的语气静默失效，而那正是这个仓库栽过的那一类。

## 事件时序（实测，不是文档 —— help.aliyun.com 在这台机器上取不到）

    run-task ──→ task-started ──→ continue-task(文本) + finish-task
                                        ↓
                     result-generated(sentence-begin / sentence-synthesis …)
                     binary 帧（裸 PCM，和元信息走两个通道）
                                        ↓
                              result-generated(sentence-end) → task-finished

三个上行事件的 `task_id` **必须是同一个**，否则服务端按「没有这个任务」拒。

## 打断

`should_stop` 在**每收到一帧之后**问一次，所以「说唤醒词打断正在播的回复」不必等整句合成完。
返回真时把已收到的音频原样交还（调用方决定播不播）—— 丢掉它等于让一次打断多花一次合成。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from core.outbound import API_USER_AGENT
from core.ws import OP_BINARY, OP_TEXT, WebSocketError, connect

#: 百炼的流式推理入口。**不是** `compatible-mode`，也不是 `/api-ws/v1/realtime`
#: （后者是 OpenAI Realtime 风格，`qwen3-*-realtime` 那族走它；这个端点对它们回
#: `ModelNotFound`，实测）。
ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

#: 裸 PCM。**不要 wav** —— 分帧下发的 wav 只有第一帧带头，拼起来就是一段带内嵌头的噪声。
FORMAT = "pcm"


class WsTtsError(RuntimeError):
    """协议层的失败。带上服务端给的 `error_code` —— `ModelNotFound` 与
    `AllocationQuota.FreeTierOnly` 是两件完全不同的事，而两者在裸 socket 层都只是「断了」。"""


def synthesize_pcm(
    *,
    model: str,
    voice: str,
    text: str,
    key: str,
    sample_rate: int = 24000,
    speed: float = 1.0,
    volume: int = 50,
    instruction: str = "",
    timeout_s: float = 30.0,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[bytes, int]:
    """一句话 → (裸 PCM 16 位小端, 第一块音频到手的毫秒)。

    第二个返回值不是装饰：「第一声要等多久」是这个产品唯一重要的延迟指标，而它只有在这一层
    量得到。调用方把它记进 `elapsed_ms` 之外的地方，好让「换了传输之后到底快了多少」不必
    重新搭一次探针。
    """
    body = str(text or "")
    if not body.strip():
        return b"", 0
    if not key:
        raise WsTtsError("没有凭据 —— 凭据走 header，不进 URL")
    task_id = uuid.uuid4().hex
    parameters: dict[str, Any] = {
        "text_type": "PlainText",
        "voice": str(voice),
        "format": FORMAT,
        "sample_rate": int(sample_rate),
        "volume": int(volume),
        "rate": float(speed),
    }
    if instruction.strip():
        # **WS 上它在 parameters 里**（HTTP 上在 input 里）。放错位置不报错，只静默失效。
        parameters["instruction"] = instruction.strip()
    started = time.monotonic()
    chunks: list[bytes] = []
    first_ms = 0
    client = connect(
        ENDPOINT,
        headers={"Authorization": f"bearer {key}", "User-Agent": API_USER_AGENT},
        timeout_s=timeout_s,
    )
    with client:
        client.send_text(
            json.dumps(
                {
                    "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio",
                        "task": "tts",
                        "function": "SpeechSynthesizer",
                        "model": str(model),
                        "parameters": parameters,
                        "input": {},
                    },
                },
                ensure_ascii=False,
            )
        )
        sent = False
        while True:
            message = client.recv()
            if message is None:
                # 对面关了。有音频就当它是完整的（服务端在 task-finished 之后立刻关很常见），
                # 没有音频才是失败 —— 「静音」和「网断了」在使用者那侧同形，必须分开报。
                if chunks:
                    break
                raise WsTtsError("连接在拿到任何音频之前就断了")
            opcode, payload = message
            if opcode == OP_BINARY:
                chunks.append(payload)
                if not first_ms:
                    first_ms = int((time.monotonic() - started) * 1000)
                if should_stop is not None and should_stop():
                    break
                continue
            if opcode != OP_TEXT:
                continue
            event = _parse(payload)
            name = str((event.get("header") or {}).get("event", ""))
            if name == "task-failed":
                header = event.get("header") or {}
                raise WsTtsError(
                    f"{header.get('error_code', '未知')}: {header.get('error_message', '')}"
                )
            if name == "task-started" and not sent:
                sent = True
                for action, extra in (("continue-task", {"text": body}), ("finish-task", {})):
                    client.send_text(
                        json.dumps(
                            {
                                "header": {
                                    "action": action,
                                    "task_id": task_id,
                                    "streaming": "duplex",
                                },
                                "payload": {"input": extra},
                            },
                            ensure_ascii=False,
                        )
                    )
                continue
            if name == "task-finished":
                break
    return b"".join(chunks), first_ms


def _parse(payload: bytes) -> dict[str, Any]:
    """一个文本帧 → dict。**解不开就当没这一帧** —— 服务端偶尔发心跳类的东西，
    而为了一行解不开的 JSON 让一次合成失败是错的取舍。"""
    try:
        parsed = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = ["ENDPOINT", "FORMAT", "WsTtsError", "synthesize_pcm"]
