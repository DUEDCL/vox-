"""The console's API surface, separated from HTTP so it can be tested plainly.

``server.py`` owns sockets, tokens and JSON framing; this owns what each endpoint
means. The split is what lets these behaviours be asserted without binding a port.

Two rules are enforced here rather than in the page, because a rule that lives in
the front end is a suggestion:

- **Security boundaries are not editable from a web page.** ``EDITABLE`` lists what
  the settings screen may change, and ``shell.enabled``, ``shell.allow``,
  ``fs.roots``, ``fs.denied_*`` and ``speaker.require_verification`` are not on it.
  Those are the four layers that stand between "a voice said something" and "a
  command ran"; turning any of them off should require opening an editor, which is
  a deliberate act, rather than clicking a toggle.

- **The console does not confirm ``shell.run``.** There is exactly one
  confirmation surface and it is the orb (FR-6.13). A second one would mean the
  same command could be approved from a window the user may not be looking at.
  The console reports that a confirmation is pending and nothing more.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from core.audio import winlevel
from core.audio.config import load_voice_config, repo_root, resolve_device
from core.audio.enroll_prompts import DEFAULT_ROUNDS, as_json as enroll_prompts_json
from core.audio.keywords import (
    MAX_KEYWORDS,
    MAX_KEYWORD_CHARS,
    MIN_KEYWORD_CHARS,
    KeywordError,
    read_keywords,
    write_keywords,
)
from core.config_edit import ConfigEditError, editable_keys, scan, set_scalars
from core.console import providers
from core.console.audio import AudioDecodeError, decode_wav_base64, quality
from core.console.logbook import Logbook
from core.models_config import (
    KINDS,
    ModelsConfigError,
    load_models_config,
    models_config_path,
    url_problem,
    write_profile_kind,
)
from core.outbound import API_USER_AGENT

#: 「试一句」与取样要求的**原始**峰值下限。
#:
#: 2026-08-31 实机：使用者什么都没说，试一句报「相似度 0.979 · 通过」。数字是真算出来的，
#: 但两边都是放大后的房间底噪 —— 环形缓冲当时存的是加过增益的样本（约 10 倍），一段静音
#: 于是看上去是 rms 0.21 的健康语音。缓冲已改为存原始音频，这条线是第二道保险：低于它的
#: 窗口和「麦克风是死的」区分不开，报错比报一个 0.979 诚实。
#:
#: 0.1 的出处是两个实测状态，它把它们分在两侧且余量都很宽：这台机器坏掉的时候，五分钟内
#: 原始峰值的**最大值**是 **0.0587**（而那期间使用者在说话）；同一台机器早先能唤醒时，
#: 块峰值是 **1.000**（那时反而是削波）。一只工作正常的麦克风说话时峰值在 0.2–0.7。
#:
#: 它是**启发式**，不是判决：它拦的是「这段窗口和一只死麦克风区分不开」，不是「这不是
#: 本人」。真正判断「这里有没有语音」该用 VAD（`models/silero_vad.onnx` 已在盘上，
#: `capture` 的 `speech_gate` 钩子还空着），那是下一步。
LIVE_MIN_PEAK = 0.10

#: 校准输入音量的目标带：说话时的**原始**峰值落在这个带里就不动它。
#:
#: 带宽的两端各有出处。下界 0.35：低于它软件增益要放大 2 倍以上，而增益放大的是信号也是
#: 底噪。上界 0.80：留 2 dB 余量给「偶尔一句说得响」—— 2026-09-01 实机的第三段注册样本
#: 峰值 1.000（削波），而削波发生在 ADC 里，任何软件增益只能等比缩小那一排平顶。
#: `CALIBRATE_TARGET` 只是报给界面的带中心（算法本身是二分，不朝某个点收敛）；它取在中间
#: 偏下，因为过冲的代价（削波，不可恢复）比欠冲（增益补一点）大得多。
CALIBRATE_BAND = (0.35, 0.80)
CALIBRATE_TARGET = 0.55
#: 最多调几轮、每轮量多久。4 × 2 s ≈ 8 秒连续说话，正好是念一句提示句的长度。
CALIBRATE_ROUNDS = 4
CALIBRATE_SECONDS = 2.0

#: How long the endpoint probe waits. Short on purpose: "this route does not work"
#: is a useful answer, and a page waiting 30 seconds for it is not.
PROBE_TIMEOUT_S = 6.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect, so the status code answers about *this* host.

    A probe that follows a 302 reports the health of whatever it landed on, which
    is the one thing this endpoint exists to disambiguate. ``None`` makes urllib
    surface the 3xx as an ``HTTPError``, and 3xx is itself a readable answer.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _probe_opener() -> urllib.request.OpenerDirector:
    """The opener the probe uses. A function so tests can replace it in one place,
    the way ``core/tools/search_backends.py`` takes an ``opener`` argument."""
    return urllib.request.build_opener(_NoRedirect)


#: 拉模型列表时最多读多少字节。正常的 ``/v1/models`` 响应是几 KB；这个上限只在
#: 对端不是它声称的那个东西时起作用 —— 无上限地读一个 body 等于把本进程的内存
#: 交给对端决定。超限就报错而不是截断后硬解：半个 JSON 解不出东西，说清楚更诚实。
MAX_MODELS_BODY = 1 << 20

#: 一次最多回多少个模型名。OpenAI 现在约 80 个，留足余量；上限存在是因为一个
#: 聚合网关能返回上千条，而那会把页面上的下拉撑成一条没法用的长卷。
MAX_MODEL_NAMES = 300

#: Anthropic 要求声明版本。缺这个头拿到的是 400，而 400 会让「密钥不对」和
#: 「请求不对」两件事在界面上看起来一样。
ANTHROPIC_VERSION = "2023-06-01"

#: 出站请求的自报身份。定义在 ``core/outbound.py`` —— http agent 走的是同一条理由，
#: 两处各留一份就会在下次改的时候只改一处。
USER_AGENT = API_USER_AGENT

#: 除了服务商预设表和 ``models.toml`` 里出现过的 ``key_env`` 之外，还允许设值的变量名。
#: 前两个不在那两处：一个是 http agent 的 token，一个是 claude CLI 走中转站的凭据。
#: ``VOX_DASHSCOPE_KEY`` 是云端 TTS（阿里云百炼 CosyVoice）的密钥 —— 它由
#: ``config/voice.toml`` 的 ``tts.key_env`` 指名，而那个文件不在 ``models.toml`` 的
#: 扫描范围里，所以要在这里点名，否则页面上存不进去。
EXTRA_SECRET_NAMES = frozenset(
    {
        "VOX_AGENT_HTTP_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "VOX_DASHSCOPE_KEY",
    }
)


def allowed_secret_names() -> frozenset[str]:
    """能通过控制台设值的环境变量名。

    **白名单，不是任意名。** 一个能设任意环境变量的网页等于能改 ``PATH``、
    ``PYTHONPATH``、``LD_PRELOAD`` —— 那是代码执行，不是配置。名字只来自三处，全都在
    本机、全都不由请求决定：服务商预设表里的 ``key_env``、``config/models.toml`` 里已经
    写下的 ``key_env``、以及上面那三个点名的。
    """
    names = set(EXTRA_SECRET_NAMES)
    for kind in KINDS:
        for preset in providers.as_json(kind):
            env = str(preset.get("key_env") or "").strip()
            if env:
                names.add(env)
    try:
        config = load_models_config(models_config_path())
    except ModelsConfigError:
        return frozenset(names)
    for profile in config["profiles"].values():
        for section in profile.values():
            if isinstance(section, Mapping):
                env = str(section.get("key_env") or "").strip()
                if env:
                    names.add(env)
    return frozenset(names)


def _fetch_headers(proto: str, key: str) -> dict[str, str]:
    """凭据怎么带，按协议分。

    没有 key 就一个认证头都不带 —— 本机服务（Ollama）不需要凭据，给它硬塞一个空
    Bearer 会换回 401，那是个假故障。
    """
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if not key:
        return headers
    if proto == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


#: 「试一句」的超时。比拉列表的宽:一次真实推理要等模型出字,而拉列表只是查表。
#: 30 秒够慢模型出 16 个 token,又不至于让页面看起来卡死。
CHAT_TIMEOUT_S = 30.0


def _chat_request(proto: str, model: str) -> tuple[str, dict[str, Any]]:
    """「试一句」的路径与请求体,按协议分。

    **这个函数此前不存在**,而 `models_try` 在调它 —— 所以「试一句」那颗按钮从来没成功过
    一次,点它换回的是 `NameError`。使用者 2026-08-29 报的「tts llm asr 试一句报错
    console failed: NameError」就是这个。全量测试当时是绿的,因为那些用例只走到
    `ApiError` 的几条早退分支(没填模型名、没有 HTTP 端点),没有一条真的走到发请求这一步。

    请求刻意做到最小:一句 ping、上限 16 个 token、不流式。够证明「这个 key 对这个模型
    有调用权」,又不至于为了一次测试烧掉可观的配额 —— 而这正是列表端点答不了的问题。
    """
    if proto == "anthropic":
        return (
            "/messages",
            {
                "model": model,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    return (
        "/chat/completions",
        {
            "model": model,
            "max_tokens": 16,
            "stream": False,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )


def _chat_reply(payload: Any) -> str:
    """从回包里取那句话。认 OpenAI 与 Anthropic 两种形状,其余返回空字符串。

    返回空而不是抛:调用方要把「通了但读不出回答」和「没通」分开报,那两件事的下一步
    不一样(一个去查响应格式,一个去查网络)。这和 `_model_names` 是同一个立场。
    """
    if not isinstance(payload, Mapping):
        return ""
    # OpenAI: choices[0].message.content（有些网关把它放在 delta 里,也认）
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            for key in ("message", "delta"):
                block = first.get(key)
                if isinstance(block, Mapping):
                    text = block.get("content")
                    if isinstance(text, str) and text.strip():
                        return text.strip()
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    # Anthropic: content[].text
    content = payload.get("content")
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping) and item.get("type") in (None, "text")
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    return ""


def _model_names(payload: Any) -> list[str]:
    """从见过的三种形状里取模型名，去重后按名字排。

    - OpenAI 兼容（绝大多数，含 Ollama 的 ``/v1`` 兼容层）：``{"data": [{"id": ...}]}``
    - Ollama 原生 ``/api/tags``：``{"models": [{"name": ...}]}``
    - 一些自建网关：顶层直接一个数组，元素是字符串或对象

    形状不认识时返回空列表而不是抛异常：调用方要把「连上了但读不出模型」和「连不上」
    分开报，这两件事的下一步动作不一样（一个去查响应格式，一个去查网络）。
    """
    rows: Any
    if isinstance(payload, Mapping):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    names: list[str] = []
    for row in rows:
        value = row.get("id") or row.get("name") or row.get("model") if isinstance(row, Mapping) else row
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return sorted(set(names))[:MAX_MODEL_NAMES]


#: Per config file, the keys the settings screen may write. Anything absent is
#: read-only from here -- see the module docstring for why these specific
#: omissions. ``input.sample_rate`` is left out for a different reason: 16 kHz is
#: an agreement between three models, and changing it means changing models.
EDITABLE: dict[str, tuple[str, ...]] = {
    "voice.toml": (
        "wake.keywords_threshold",
        "wake.num_threads",
        "asr.enabled",
        "asr.num_threads",
        "tts.enabled",
        # 2026-08-29:合成的 provider / 模型 / 音色可从页面改 —— 这正是「控制台不能配置
        # 云端 TTS」缺的最后一环(前三环是:没有云端 provider、schema 没有这几个键、
        # models.toml 没有读侧)。
        #
        # **`tts.key_env` 刻意不在这里。** 它是「去读哪个环境变量」,让网页改它等于让网页
        # 决定把哪个凭据发给百炼 —— 例如指到 ANTHROPIC_AUTH_TOKEN 上。密钥的**值**走
        # /api/secret(白名单校验),变量**名**留在文件里。
        "tts.provider",
        "tts.model",
        "tts.voice",
        # instruction 只有 qwen-audio-3.0-tts-* 支持。它是把声音调成「温柔」的正确杠杆:
        # 音色决定是谁在说,这一行决定她怎么说。
        "tts.instruction",
        "tts.speaker_id",
        "tts.speed",
        "tts.num_threads",
        "input.device",
        "input.blocksize",
        "orb.enabled",
        "orb.visible",
    ),
    "speaker.toml": (
        "speaker.threshold",
        "speaker.min_verify_seconds",
        "speaker.min_enroll_seconds",
        "speaker.min_rms",
        "speaker.max_clip_ratio",
        "speaker.verify_windows",
        "speaker.max_consecutive_rejections",
        "speaker.cooldown_s",
        "capture.buffer_seconds",
        "capture.verify_seconds",
    ),
    "tools.toml": (
        "web.max_results",
        "web.snippet_chars",
        "web.searx_url",
        "web.allow_internet",
        "web.timeout_s",
        "fs.max_bytes",
    ),
    "memory.toml": ("memory.recall_limit", "memory.short_keep"),
}

#: Per-agent keys the settings screen may write, by key name (the section carries
#: an index: ``agents[0].enabled``). The omissions are the whole point:
#:
#: - ``command`` / ``args`` / ``cwd`` decide **which executable runs**. A web page
#:   that can change them is a remote code execution primitive, and no amount of
#:   loopback binding makes that acceptable.
#: - ``url`` decides **where data goes**. Loopback validation protects the plain
#:   HTTP case; it does not stop somebody pointing an agent at an https endpoint
#:   they control.
#: - ``env_passthrough`` decides **which credentials the child inherits**.
#: - ``name`` is the key the router's success statistics are recorded against, and
#:   ``kind`` chooses the adapter class. Renaming or re-typing an entry is an edit
#:   to the shape of the registry, not to its settings.
AGENT_EDITABLE = frozenset({"enabled", "cost", "latency_ms", "timeout_s", "model", "capabilities"})

#: What the settings screen may write in ``config/mcp.toml``. The omissions follow
#: the same rule as the agent registry's, applied to a wider blast radius:
#:
#: - ``require_confirmation`` is the last gate before a remote tool runs. Turning
#:   it off from a web page is the same category of act as turning off the
#:   voiceprint gate, and it is refused for the same reason.
#: - ``servers[N].allow`` can only *widen* when edited (an empty list means every
#:   tool the server offers), and ``auto_allow`` is literally the confirmation
#:   bypass list. Neither belongs behind a toggle.
#: - ``command`` / ``args`` / ``cwd`` / ``env_passthrough`` choose what executes and
#:   with which credentials.
#:
#: What is left is the master switch, the limits, and each server's own on/off --
#: enough to turn MCP on and off from the page without moving any boundary.
MCP_EDITABLE: tuple[str, ...] = ("mcp.enabled", "mcp.timeout_s", "mcp.max_output_bytes")
MCP_SERVER_EDITABLE = frozenset({"enabled"})

#: A facts file name: no separators, no ``..``, ``.md`` only. Chinese is allowed
#: because ``fact_slug`` produces it.
_FACT_NAME = re.compile(r"^[\w一-鿿][\w一-鿿 .\-]{0,120}\.md$")

#: Which loader validates which file, so a rejected edit never lands on disk.
_VALIDATORS: dict[str, str] = {
    "voice.toml": "voice",
    "speaker.toml": "speaker",
    "tools.toml": "tools",
    "memory.toml": "memory",
}


class ApiError(RuntimeError):
    """A request the API refuses, with a message meant for the page."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _section_order(section: str) -> tuple[str, int]:
    """Sort ``agents[10]`` after ``agents[9]`` rather than lexically."""
    match = re.match(r"^(?P<table>.+)\[(?P<index>\d+)\]$", section)
    if not match:
        return (section, -1)
    return (match.group("table"), int(match.group("index")))


def _agent_key_allowed(key: str) -> bool:
    """``agents[0].enabled`` -> allowed; ``agents[0].command`` -> refused."""
    match = re.match(r"^agents\[\d+\]\.(?P<name>[A-Za-z0-9_-]+)$", key)
    return bool(match) and match.group("name") in AGENT_EDITABLE


def _mcp_key_allowed(key: str) -> bool:
    """``mcp.enabled`` and ``servers[N].enabled`` only."""
    if key in MCP_EDITABLE:
        return True
    match = re.match(r"^servers\[\d+\]\.(?P<name>[A-Za-z0-9_-]+)$", key)
    return bool(match) and match.group("name") in MCP_SERVER_EDITABLE


def _fact_title(text: str) -> str:
    """The first real heading, skipping the front matter ``mirror_fact`` writes.

    Without the skip every synced file titles itself ``---``: the Markdown mirror
    carries a YAML block so a hand edit can be folded back to the right record, and
    that block is the first thing in the file.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        closing = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), 0)
        start = closing + 1
    for line in lines[start:]:
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("# ").strip()
    return ""


def _validator(kind: str):
    """The config's own loader, as a callable that raises on a bad candidate."""

    def validate(path: Path) -> None:
        if kind == "voice":
            from core.audio.config import load_voice_config as loader
        elif kind == "speaker":
            from core.audio.speaker import load_speaker_config as loader
        elif kind == "tools":
            from core.tools.policy import load_tools_config as loader
        else:
            from core.memory.store import load_memory_config as loader
        loader(path)

    return validate


class ConsoleApi:
    """Everything the page can ask for, over one runtime and one voice stack."""

    def __init__(self, runtime: Any, stack: Any = None, *, config_dir: Path | None = None) -> None:
        self.runtime = runtime
        self.stack = stack
        self.config_dir = config_dir or (repo_root() / "config")
        self.started_at = time.time()
        #: Set by ``mic_start`` so ``mic_stop`` and ``state`` can tell whether the
        #: capture this API opened is the one still running.
        self.mic_running = False
        #: 由启动脚本注入的重启入口，签名是 ``(delay_s: float) -> None``。
        #:
        #: 注入而不是在这里实现：怎么替换一个进程（收球、关 socket、``execv``）是启动脚本
        #: 的知识，而 ``ConsoleApi`` 只知道「有人按了重启」。没注入时 ``restart()`` 报 501
        #: 而不是假装成功 —— 一个点了没反应的重启按钮比一个明确说「这里不支持」的更难查。
        self.restart_hook: Any = None
        #: 从**采集环形缓冲**取来的注册片段（内存里，永不落盘、永不出进程）。
        #:
        #: 为什么不是浏览器录的：注册和校验必须走**同一条信道**，否则相似度比的是两个
        #: 录音链路的差别而不是两个人的差别。浏览器 `getUserMedia` 拿的是**浏览器认为的
        #: 默认设备**（不是 `[input] device`），还带它自己的 AGC / 降噪 / 回声消除和它自己
        #: 的采样率；Vox 校验时读的是 `SounddeviceWakeCapture` 那条流。两条不同的链路
        #: 各自都「录成功了」，而门比出来的分数没有意义。
        #:
        #: 现在这几段就是**门读的那个缓冲**里的样本（`capture._ring`），所以「同一条信道」
        #: 不是靠约定，是构造上就成立的。
        self._enroll_clips: list[Any] = []
        #: 运行日志。``runtime.logbook`` 指向同一个对象 —— 写的人是派发器和 runtime，
        #: 读的人是这个 API。已经有一个就用它，免得两边各记一半。
        existing = getattr(runtime, "logbook", None)
        self.logbook = existing if existing is not None else Logbook()
        try:
            runtime.logbook = self.logbook
        except Exception:  # noqa: BLE001 - 一个假 runtime 不该让控制台起不来
            pass

    # ------------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        """One call that answers "what is wired, and what is still missing".

        Counts, names and readiness only: no tokens, no vectors, no memory text,
        no command text. The orb's confirmation card is the only place a pending
        command is shown in full.
        """
        runtime_view: dict[str, Any] = {}
        try:
            runtime_view = self.runtime.describe()
        except Exception as exc:  # noqa: BLE001 - the console must still render
            runtime_view = {"error": type(exc).__name__}
        payload: dict[str, Any] = {
            "uptime_s": round(time.time() - self.started_at, 1),
            "runtime": runtime_view,
            "state": getattr(self.runtime.plugin.machine.state, "value", "unknown"),
            "turns": getattr(self.runtime, "turns", 0),
            "mic_running": self.mic_running,
            "input_level": self._input_level(),
            "wake": self._wake_funnel(),
            "readiness": self.stack.readiness() if self.stack is not None else [],
            "warnings": list(self.stack.warnings) if self.stack is not None else [],
            "pending_confirmation": self._pending_confirmation(),
        }
        try:
            payload["voice_config"] = {
                key: value
                for key, value in load_voice_config().items()
                if not key.endswith("_dir") and key != "vad_model"
            }
        except Exception as exc:  # noqa: BLE001
            payload["voice_config"] = {"error": type(exc).__name__}
        return payload

    def _pending_confirmation(self) -> dict[str, Any] | None:
        """That one is waiting, never what it is. The orb shows the command."""
        pending = getattr(self.runtime, "_pending_confirm", None)
        if not pending:
            return None
        return {"tool": (pending.get("payload") or {}).get("tool", "shell.run"), "where": "orb"}

    def _wake_funnel(self) -> dict[str, Any]:
        """唤醒漏斗：命中 / 接受 / 拒绝 / 接受了但没进聆听，加最近几次尝试。

        **四层必须分开报。** 「喊了没反应」有四个完全不同的根因，而它们在使用者眼里
        长得一模一样：麦克风没进声音、唤醒词没命中、声纹把它拒了、以及**接受了但识别器
        没开起来**。实机诊断读出「KWS 命中 16/16、声纹接受 0/16」正是靠这个分离 ——
        合成一个数字就什么也答不了。

        ``muted`` 是确认音期间被丢掉的音频块数（见 `core/audio/capture.py` 的静音窗）。
        它正常时是个不大的数；如果它一直涨，说明窗口没被收回来，而那个症状是「完全没
        反应」—— 那时这一格是唯一的读数。

        带相似度和原因，不带音频、不带向量。这份数据只到本机控制台（和运行日志同一个
        扇出面），不进事件流。
        """
        stats = dict(getattr(self.runtime, "wake_stats", {}) or {})
        recent = list(getattr(self.runtime, "wake_recent", []) or [])
        capture = self.stack.capture if self.stack is not None else None
        return {
            "kws": int(stats.get("kws", 0)),
            "accepted": int(stats.get("accepted", 0)),
            "rejected": int(stats.get("rejected", 0)),
            "listen_refused": int(stats.get("listen_refused", 0)),
            "last_listen_refusal": str(getattr(capture, "last_listen_refusal", "") or ""),
            "muted": int(getattr(capture, "muted_blocks", 0) or 0),
            # 「喊了没反应」的第五个根因，也是唯一一个**不是故障**的：注册模式。一个人都
            # 没注册时麦克风照常跑、电平照常涨，但唤醒判定被按住 —— 上面四个计数会全是 0
            # 而每一层看起来都健康。不报出来的话，这个状态和「KWS 装不上」长得一样。
            "enroll_only": bool(getattr(capture, "enroll_only", False)),
            "held": int(getattr(capture, "wake_holds", 0) or 0),
            "recent": [dict(entry) for entry in recent[:20]],
        }

    def _input_level(self) -> dict[str, Any] | None:
        """麦克风到底有没有在出声。``None`` = 还没开麦。

        为什么这一项值得占就绪清单一格：Windows 上一个被静音、被隐私设置拒绝、或者根本
        不在用的输入设备**不报错** —— 流照常打开、回调照常触发、样本全是零。表现是
        「唤醒词唤不醒」，而配置、词表、模型、声纹每一层都显示健康。实测本机默认设备
        peak 是 0.00003（数值噪声），同一时刻另一个设备是 0.027。

        报的是峰值而不是 RMS：安静房间的 RMS 也很低，但峰值有噪声底；全零的设备两个都没有。
        """
        capture = self.stack.capture if self.stack is not None else None
        if capture is None or not self.mic_running:
            return None
        peak = float(getattr(capture, "input_peak", 0.0) or 0.0)
        blocks = int(getattr(capture, "input_blocks", 0) or 0)
        rate = int(getattr(capture, "sample_rate", 16000) or 16000)
        size = int(getattr(capture, "blocksize", 1600) or 1600)
        gain = getattr(capture, "auto_gain", None)
        selector, device_name = self._device_in_use()
        os_side: dict[str, Any] | None = None
        os_reason = ""
        if device_name:
            try:
                os_side = winlevel.read_level(device_name).describe()
            except Exception as exc:  # noqa: BLE001 - 读不到就说读不到
                os_reason = str(exc)
        return {
            "peak": round(peak, 6),
            "seconds": round(blocks * size / max(1, rate), 1),
            "silent": bool(getattr(capture, "input_silent", False)),
            # **在用哪只设备，报名字。** 索引会漂（实测 `device = "2"` 从「耳机」变成了
            # 「麦克风阵列」），而一个只报索引的界面让这件事永远不可见。
            "device": device_name or (None if selector is None else str(selector)),
            # **Windows 那一侧的输入音量。** 同一时刻实测「耳机」0.01、「麦克风阵列」0.82
            # —— 「设备坏了」和「音量是 1%」此前在界面上长得一模一样。
            "os": os_side,
            "os_reason": os_reason,
            # **原始峰值够不够用**，单独报一格。0.0587 这种量级和「麦克风是死的」区分不开，
            # 而在缓冲存加增益样本的那个版本里，它被 10 倍增益盖成了一段「健康语音」。
            "too_quiet": bool(peak and peak < LIVE_MIN_PEAK),
            "want_peak": LIVE_MIN_PEAK,
            # 自适应增益在放多少倍。**这个数字必须能看见**：它越大说明设备来的越轻，
            # 而它放大的是信号也是底噪 —— 增益爬到 10 倍以上时下游看到的「语音」很可能
            # 只是被抬起来的房间。
            "gain": (gain.describe() if gain is not None and hasattr(gain, "describe") else None),
        }

    def events(self, since: int = 0) -> dict[str, Any]:
        """Envelopes the runtime has seen, from ``since`` onward.

        These are already-validated envelopes and they are already redacted by
        design: ``task.*`` carries no user text, ``memory.*`` carries no memory
        text, and ``tool.*`` carries a decision rather than output.
        """
        seen = list(getattr(self.runtime, "seen", []))
        start = max(0, min(int(since or 0), len(seen)))
        return {"next": len(seen), "events": seen[start:]}

    def agents(self) -> dict[str, Any]:
        """Per-agent availability, as ``check()`` reports it.

        A configured agent whose command is not on PATH stays in this list. Dropping
        it would make "one agent fewer" and "one agent misconfigured" look the same.
        """
        rows = []
        for name, adapter in sorted(getattr(self.runtime, "adapters", {}).items()):
            row: dict[str, Any] = {"name": name}
            try:
                status = adapter.check()
                row["available"] = bool(status.get("available", True))
                row["reason"] = str(status.get("reason", "") or "")
            except Exception as exc:  # noqa: BLE001
                row["available"] = False
                row["reason"] = f"check failed: {type(exc).__name__}"
            rows.append(row)
        return {"agents": rows}

    # ------------------------------------------------------------------ config

    def config_view(self) -> dict[str, Any]:
        """Every editable key across the four config files, with current values.

        Files that exist but have no editable keys still appear, so the page can
        say "nothing here is editable from the console" rather than hiding it.
        """
        files = []
        for name, allowed in EDITABLE.items():
            path = self.config_dir / name
            if not path.is_file():
                files.append({"file": name, "present": False, "keys": []})
                continue
            try:
                keys = editable_keys(path, allow=allowed)
            except Exception as exc:  # noqa: BLE001 - a broken file must still render
                files.append({"file": name, "present": True, "error": type(exc).__name__, "keys": []})
                continue
            files.append(
                {
                    "file": name,
                    "present": True,
                    "keys": [
                        {
                            "key": entry["key"],
                            "value": entry["value"],
                            "type": type(entry["value"]).__name__,
                            "editable": entry["editable"],
                        }
                        for entry in keys
                    ],
                }
            )
        return {"files": files}

    def config_update(self, file: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Write allowed keys, validated, atomically. Anything else is refused.

        The allow-list check happens before the write, so a request naming
        ``shell.enabled`` is refused rather than validated-then-refused: the reason
        it is out of bounds has nothing to do with whether the value would parse.
        """
        if file not in EDITABLE:
            raise ApiError(f"not a console-editable config file: {file}")
        allowed = set(EDITABLE[file])
        rejected = sorted(set(updates) - allowed)
        if rejected:
            raise ApiError(
                "these keys are not editable from the console (edit the file): "
                + ", ".join(rejected),
                status=403,
            )
        if not updates:
            return {"changed": {}}
        path = self.config_dir / file
        try:
            changed = set_scalars(path, updates, validate=_validator(_VALIDATORS[file]))
        except ConfigEditError as exc:
            raise ApiError(str(exc)) from exc
        return {"changed": changed, "restart_required": True}

    # ------------------------------------------------------------------- models

    def models_view(self) -> dict[str, Any]:
        """The profiles, plus the provider presets the dropdowns are built from.

        No key values, because the file cannot contain any: only ``key_env``, the
        *name* of an environment variable. The presets come from
        ``core/console/providers.py`` so the page does not carry a second copy of
        the endpoint table -- it has a fallback for when this endpoint is missing,
        and ``Object.assign`` lets this one win.
        """
        path = models_config_path()
        try:
            config = load_models_config(path)
        except ModelsConfigError as exc:
            # The message leads with the real cause. Rendering an empty registry
            # instead would look like "no profiles configured", which is a
            # different fact entirely.
            raise ApiError(f"config/models.toml 有问题：{exc}", status=500) from exc
        return {
            "present": path.is_file(),
            "active": config["active"],
            "profiles": config["profiles"],
            "presets": {kind: providers.as_json(kind) for kind in KINDS},
        }

    def models_update(
        self, profile: str, kind: str, fields: Mapping[str, Any], label: str = ""
    ) -> dict[str, Any]:
        """Write one role of one profile, creating the profile when it is new.

        ``models.toml`` is not on ``EDITABLE`` on purpose: its tables are data, so
        it gets this endpoint with its own allow-list (``FIELDS``) instead of a
        second door through the generic config editor, which would validate the
        same write more weakly.
        """
        if not isinstance(fields, Mapping):
            raise ApiError("fields must be an object")
        # What the provider table already says. Passing it keeps the file from
        # growing a second copy of an endpoint that lives in ``providers.py``: the
        # page fills these in from the preset for display, and persisting them
        # would pin today's value where tomorrow's correction cannot reach.
        # ``custom`` is exempt -- there the file *is* the source of truth.
        preset = providers.find(str(kind), str(fields.get("provider", "")))
        defaults = None
        if preset is not None and preset.slug != "custom":
            defaults = {"base": preset.base, "proto": preset.proto, "key_env": preset.key_env}
        try:
            changed = write_profile_kind(
                str(profile),
                str(kind),
                fields,
                path=models_config_path(),
                label=str(label or ""),
                preset=defaults,
            )
        except (ModelsConfigError, ConfigEditError) as exc:
            raise ApiError(str(exc)) from exc
        return {"changed": changed, "restart_required": True}

    def models_probe(self, kind: str, provider: str = "", base: str = "") -> dict[str, Any]:
        """Ask an endpoint whether it is there: one ``GET {base}/models``.

        This is the only outbound request the console makes, and it is the only
        evidence that says which of three things is wrong: ``401``/``403`` means the
        host is up and the path is right and only the key is missing, ``404`` means
        the path is wrong, a timeout means the route does not work at all. **No
        credential is sent** -- that is what makes ``401`` the good answer rather
        than an ambiguous one.

        The status code is the whole result. The body is never read: it can be
        large, and nothing here needs it.
        """
        endpoint_base = str(base or "").strip()
        if not endpoint_base:
            preset = providers.find(str(kind), str(provider))
            endpoint_base = (preset.base if preset else "") or ""
        if not endpoint_base:
            raise ApiError("这一条没有 HTTP 端点可探（本机服务）")
        problem = url_problem(endpoint_base)
        if problem:
            raise ApiError(f"不探这个端点：{problem}")
        url = endpoint_base.rstrip("/") + "/models"
        request = urllib.request.Request(
            url,
            method="GET",
            # 同一个 UA 理由：默认的 ``Python-urllib`` 会被网关按反爬规则 403，而探测把
            # 状态码当结论，一个假的 403 会被读成「密钥不对」。
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        opener = _probe_opener()
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=PROBE_TIMEOUT_S) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            # The useful cases arrive here: 401, 403, 404, and a 3xx that
            # ``_NoRedirect`` refused to follow.
            status = exc.code
            exc.close()
        except (urllib.error.URLError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            reason = getattr(exc, "reason", exc)
            raise ApiError(
                f"连不上：{type(exc).__name__}: {reason}（{elapsed}ms 后放弃）", status=502
            ) from exc
        return {
            "url": url,
            "status": int(status),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def models_fetch(
        self,
        kind: str,
        provider: str = "",
        base: str = "",
        key_env: str = "",
        proto: str = "",
    ) -> dict[str, Any]:
        """``GET {base}/models``，这次带凭据、读 body，把可选的模型名解析出来。

        与 ``models_probe`` 的分工不是重复：探测**不带凭据**，所以 ``401`` 是它的好
        结果；拉列表必须带凭据，所以 ``401`` 在这里是失败。两件事合成一个端点就得在
        「要不要发密钥」上二选一，而不发正是探测刻意做的那个选择。

        **密钥只从环境变量读。** 入参 ``key_env`` 是变量**名**，页面从不传值 —— 与
        ``config/models.toml`` 同一条规矩。响应里也没有它：回的是模型名、状态码、耗时，
        和「带没带凭据」这一个布尔。
        """
        preset = providers.find(str(kind), str(provider))
        endpoint_base = str(base or "").strip() or ((preset.base if preset else "") or "")
        if not endpoint_base:
            raise ApiError(
                "这一条没有 HTTP 端点可拉 —— 本机服务的模型在 models/ 目录里，不由端点列举"
            )
        problem = url_problem(endpoint_base)
        if problem:
            raise ApiError(f"不拉这个端点：{problem}")
        shape = str(proto or "").strip() or ((preset.proto if preset else "") or "openai")
        env_name = str(key_env or "").strip() or ((preset.key_env if preset else "") or "")
        # 变量名进来，值在这里读。页面上那个框填的是名字,所以这一行是密钥唯一的入口。
        key = os.environ.get(env_name, "").strip() if env_name else ""
        url = endpoint_base.rstrip("/") + "/models"
        request = urllib.request.Request(
            url, method="GET", headers=_fetch_headers(shape, key)
        )
        opener = _probe_opener()
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=PROBE_TIMEOUT_S) as response:
                status = int(response.status)
                # 多读一个字节:读满上限说明还有更多,那正是要报错的情形。
                raw = response.read(MAX_MODELS_BODY + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            if status in (401, 403):
                hint = (
                    f"密钥被拒（值读自环境变量 ${env_name}）"
                    if key
                    else f"要密钥：设好环境变量 ${env_name} 再拉一次"
                    if env_name
                    else "要密钥，但这一条没有填密钥环境变量名"
                )
                raise ApiError(f"{status} —— {hint}", status=502) from None
            if status == 404:
                raise ApiError(f"404 —— 主机在，但 {url} 这个路径不对", status=502) from None
            raise ApiError(f"{status} —— 端点拒绝了这次请求", status=502) from None
        except (urllib.error.URLError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            reason = getattr(exc, "reason", exc)
            raise ApiError(
                f"连不上：{type(exc).__name__}: {reason}（{elapsed}ms 后放弃）", status=502
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if len(raw) > MAX_MODELS_BODY:
            raise ApiError(
                f"响应超过 {MAX_MODELS_BODY} 字节就不读了 —— /models 正常只有几 KB，"
                "这个体量说明对端返回的不是模型列表",
                status=502,
            )
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ApiError(f"{status} 通了，但响应不是 JSON：{exc}", status=502) from exc
        names = _model_names(body)
        if not names:
            raise ApiError(
                f"{status} 通了，但响应里找不到模型列表 —— 认得 data[].id、models[].name "
                "和顶层数组三种形状，这个三种都不是",
                status=502,
            )
        return {
            "url": url,
            "status": status,
            "elapsed_ms": elapsed_ms,
            # 带没带凭据,不是凭据本身。界面要用它区分「匿名就能列」和「靠密钥列到的」。
            "authenticated": bool(key),
            "key_env": env_name,
            "count": len(names),
            "models": names,
        }

    def models_try(
        self,
        kind: str,
        provider: str = "",
        base: str = "",
        key_env: str = "",
        proto: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """真的发一句话过去，把回来的那句话原样带回。

        **这是「这个配置能用」的唯一判据。** 能列出 ``/models`` 不等于能对话：列表端点和
        推理端点常常在不同的网关后面，配额和权限也常常分开算 —— 一个能列出四个模型、
        对哪个都没有调用权的 key 在拉取那一步看起来完全正常。

        请求刻意做到最小：一句 ``ping``、上限 16 个 token、不流式。够证明链路通，
        又不会为了一次测试烧掉可观的配额。
        """
        wanted = str(model or "").strip()
        if not wanted:
            raise ApiError("要先填模型名 —— 试一句话得指定拿哪个模型试")
        preset = providers.find(str(kind), str(provider))
        endpoint_base = str(base or "").strip() or ((preset.base if preset else "") or "")
        if not endpoint_base:
            raise ApiError("这一条没有 HTTP 端点可试（本机服务不走 HTTP）")
        problem = url_problem(endpoint_base)
        if problem:
            raise ApiError(f"不试这个端点：{problem}")
        shape = str(proto or "").strip() or ((preset.proto if preset else "") or "openai")
        env_name = str(key_env or "").strip() or ((preset.key_env if preset else "") or "")
        key = os.environ.get(env_name, "").strip() if env_name else ""
        path, body = _chat_request(shape, wanted)
        url = endpoint_base.rstrip("/") + path
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={**_fetch_headers(shape, key), "Content-Type": "application/json"},
        )
        opener = _probe_opener()
        started = time.perf_counter()
        try:
            with opener.open(request, timeout=CHAT_TIMEOUT_S) as response:
                status = int(response.status)
                raw = response.read(MAX_MODELS_BODY + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            detail = ""
            try:
                # 错误 body 里通常写着真正的原因（模型名不对、配额用完、渠道未开），
                # 而状态码只说「不行」。截断到 300 字符：够读，不至于把一页 HTML 倒出来。
                detail = exc.read(2000).decode("utf-8", errors="replace").strip()[:300]
            except Exception:  # noqa: BLE001 - 读不到就算了，状态码仍然有用
                pass
            exc.close()
            hint = f"{status}"
            if status in (401, 403):
                hint += f" —— 密钥{'被拒' if key else f'没带（环境变量 ${env_name} 是空的）'}"
            elif status == 404:
                hint += f" —— {url} 这个路径不对"
            raise ApiError(f"{hint}{chr(10) + detail if detail else ''}", status=502) from None
        except (urllib.error.URLError, OSError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            reason = getattr(exc, "reason", exc)
            raise ApiError(
                f"连不上：{type(exc).__name__}: {reason}（{elapsed}ms 后放弃）", status=502
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ApiError(f"{status} 通了，但响应不是 JSON：{exc}", status=502) from exc
        reply = _chat_reply(payload)
        if not reply:
            raise ApiError(
                f"{status} 通了，但响应里没有回答文本 —— 认得 OpenAI 的 "
                "choices[].message.content 和 Anthropic 的 content[].text，这个都不是",
                status=502,
            )
        return {
            "url": url,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "model": wanted,
            "authenticated": bool(key),
            "key_env": env_name,
            "reply": reply,
        }

    # ------------------------------------------------------------------------ log

    def log_view(self, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        """运行日志，从游标之后读。

        这是「``route=tool ok=false tool=fs.read 0ms``」这类报告唯一查得下去的地方：事件流
        按契约不带参数（它扇出到球、传输和每个消费者），所以哪个 path 被谁拒了只在这里。
        """
        try:
            return self.logbook.read(int(cursor), int(limit))
        except (TypeError, ValueError) as exc:
            raise ApiError(f"cursor/limit 得是整数：{exc}") from exc

    def log_clear(self) -> dict[str, Any]:
        self.logbook.clear()
        return {"cleared": True}

    # -------------------------------------------------------------------- restart

    def restart(self, delay_s: float = 0.7) -> dict[str, Any]:
        """整个重启：控制台、语音栈、agent 注册表、唤醒球，全部按新配置重建。

        唤醒词表、模型方案、agent 配置都是**启动时**读的，所以「改完生效」只有这一条路。
        热重载不做：KWS / ASR / TTS 三个模型和麦克风流都是启动时建的，替换它们要处理
        「换的时候正在说话」这种状态，比重启贵得多也脆得多。

        延时是给 HTTP 响应留出发出去的时间 —— 进程在响应写完之前被替换，页面看到的是
        连接被掐断，那和「重启失败」长得一样。
        """
        if self.restart_hook is None:
            raise ApiError(
                "这个控制台没有装重启入口（只有 scripts/run_console.py 起的才有）", status=501
            )
        threading.Thread(
            target=self.restart_hook, args=(float(delay_s),), daemon=True, name="vox-restart"
        ).start()
        return {"restarting": True, "delay_s": float(delay_s)}

    # -------------------------------------------------------------------- secrets

    def secrets_view(self) -> dict[str, Any]:
        """哪些凭据变量**已经有值**，以及还能设哪些名字。值一个都不回。"""
        from core.env_file import DEFAULT_ENV_FILE
        from core.env_file import workspace_root as env_root

        names = sorted(allowed_secret_names())
        return {
            "names": names,
            "present": [name for name in names if os.environ.get(name, "").strip()],
            "env_file": str(env_root() / DEFAULT_ENV_FILE),
        }

    def secret_set(self, name: str, value: str, remember: bool = False) -> dict[str, Any]:
        """给一个凭据变量设值。默认只进本进程环境，勾了 ``remember`` 才写 ``.env``。

        默认不落盘：「试一个服务商」和「定下来用它」是两件事 —— 前者不该在磁盘上留一个
        key，后者需要重启之后它还在。

        名字必须在白名单里，理由见 ``allowed_secret_names``。
        """
        from core.env_file import set_env_value

        key = str(name).strip()
        if key not in allowed_secret_names():
            raise ApiError(
                f"{key!r} 不在可设的变量名里 —— 能设的只有服务商预设表和 "
                "config/models.toml 里出现过的 key_env。白名单之外的名字会让这个页面能改 "
                "PATH 之类的东西，那是代码执行不是配置"
            )
        secret = str(value)
        if not secret.strip():
            raise ApiError("密钥不能是空的")
        os.environ[key] = secret
        if remember:
            set_env_value(key, secret)
        # 回的是决定，不是值。
        return {"name": key, "present": True, "remembered": bool(remember)}

    def secret_clear(self, name: str) -> dict[str, Any]:
        """从本进程环境里删掉一个凭据。

        ``.env`` 里那一行不动：删磁盘上的东西要用编辑器 —— 一个能删凭据文件内容的网页
        比一个只影响当前进程的按钮危险得多，而「这一轮不想用它了」只需要后者。
        """
        key = str(name).strip()
        if key not in allowed_secret_names():
            raise ApiError(f"{key!r} 不在可设的变量名里")
        os.environ.pop(key, None)
        return {"name": key, "present": False}

    # ---------------------------------------------------------------------- wake

    def wake_view(self) -> dict[str, Any]:
        """现在能喊什么，以及这份表是从哪来的。

        「唤不醒」最常见的原因是说了个不在表里的词 —— 模型自带 8 个词，而这份表在界面上
        一直没有出口，用户只能靠猜。所以这个端点先回答「现在喊哪个词有用」，改词是第二位。
        """
        from core.audio.config import custom_keywords_path, model_paths, resolve_keywords_file

        config = load_voice_config()
        model_dir = model_paths()["kws_dir"]
        shipped = model_dir / "keywords.txt"
        custom = custom_keywords_path()
        active = resolve_keywords_file(config) or shipped
        return {
            "active_path": str(active),
            "custom_path": str(custom),
            "shipped_path": str(shipped),
            # 生效的是自定义那份还是模型出厂那份。界面要用它决定「恢复出厂」显不显示。
            "custom": active == custom,
            "words": read_keywords(active),
            "shipped_words": read_keywords(shipped),
            "threshold": config["wake.keywords_threshold"],
            "limits": {
                "min_chars": MIN_KEYWORD_CHARS,
                "max_chars": MAX_KEYWORD_CHARS,
                "max_words": MAX_KEYWORDS,
            },
            # 词表是启动时喂给 KWS 的,改完必须重启 —— 不说清楚会让用户改完对着麦克风
            # 反复喊一个还没生效的词。
            "restart_required": True,
        }

    # ---------------------------------------------------------------- 合成音色

    def voices_view(self) -> dict[str, Any]:
        """云端 TTS 能选哪些音色，以及这份表是**从哪来的**。

        使用者要求「tts 模型配置应该能拉取到目标模型的音色」。核实结果是：**百炼没有
        列举系统预置音色的 API**（有列表 API 的是声音复刻，只列你自己复刻的）。所以这里
        回的是一张带出处与核实日期的钉住表，而界面把它做成**可输入的建议列表**而不是
        封闭下拉 —— 表里没有的音色照样能填，由「试一句」去判，不由这张表判合法性。

        这个区别必须在响应里就说出来（``source`` / ``checked`` / ``live``），否则界面上
        一个下拉框会让人以为它是实时的。
        """
        from core.audio.voices import (
            CLOUD_TTS_MODELS,
            INSTRUCTION_MODELS,
            VOICE_LIST_CHECKED,
            VOICE_LIST_SOURCE,
            voice_options,
        )

        config = load_voice_config()
        key_env = str(config.get("tts.key_env", "VOX_DASHSCOPE_KEY")) or "VOX_DASHSCOPE_KEY"
        return {
            "provider": str(config.get("tts.provider", "sherpa")),
            "model": str(config.get("tts.model", "")),
            "voice": str(config.get("tts.voice", "")),
            "models": list(CLOUD_TTS_MODELS),
            # 按当前 model 给对应的一组 —— 混用音色会 411,给「全部音色」的下拉是在邀请报错。
            "voices": voice_options(str(config.get("tts.model", ""))),
            "instruction": str(config.get("tts.instruction", "")),
            "supports_instruction": str(config.get("tts.model", "")) in INSTRUCTION_MODELS,
            "key_env": key_env,
            "key_present": bool(os.environ.get(key_env, "").strip()),
            # 这三个字段是诚实性的一部分,不是装饰。
            "live": False,
            "source": VOICE_LIST_SOURCE,
            "checked": VOICE_LIST_CHECKED,
            "restart_required": True,
        }

    def voice_try(self, text: str = "", model: str = "", voice: str = "") -> dict[str, Any]:
        """用指定的模型 + 音色真合成一句，报耗时与采样数。**不落盘、不进日志正文。**

        这是「拉取音色」那张表唯一的验证手段：表是钉住的，可能过期；一次真实合成不会。
        所以配错的组合（例如 v1 的音色名配 v2 的模型）在这里就会以 HTTP 4xx 出现，
        而不是等到唤醒之后才发现不出声。
        """
        from core.audio.tts_cloud import DashScopeTtsError, DashScopeTtsProvider

        config = load_voice_config()
        key_env = str(config.get("tts.key_env", "VOX_DASHSCOPE_KEY")) or "VOX_DASHSCOPE_KEY"
        say = str(text or "").strip() or "你好，我是沃，这是一次音色试听。"
        provider = DashScopeTtsProvider(
            model=str(model or config.get("tts.model", "") or "cosyvoice-v2"),
            voice=str(voice or config.get("tts.voice", "") or "longyuan"),
            key_env=key_env,
        )
        status = provider.load()
        if not status.available:
            return {"ok": False, "reason": status.details["reason"], "key_env": key_env}
        try:
            audio = provider.synthesize(say)
        except (DashScopeTtsError, Exception) as exc:  # noqa: BLE001 - 原因要显示出来
            return {
                "ok": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "model": provider.model,
                "voice": provider.voice,
            }
        return {
            "ok": True,
            "model": provider.model,
            "voice": provider.voice,
            "elapsed_ms": audio.elapsed_ms,
            "sample_rate": audio.sample_rate,
            "samples": int(len(audio.samples)),
            "seconds": round(len(audio.samples) / max(1, audio.sample_rate), 2),
            # 试听的音频**不返回给页面**：一次 wav 是几百 KB 的 base64,而这个端点要回答的
            # 是「这个组合通不通」。要听就用「说一句」那条路,它走真实播放链。
            "chars": len(say),
        }

    def wake_update(self, words: Sequence[Any]) -> dict[str, Any]:
        """整份换掉自定义词表。空列表 = 删掉它，回落到模型出厂那份。"""
        from core.audio.config import custom_keywords_path, model_paths

        if not isinstance(words, Sequence) or isinstance(words, (str, bytes)):
            raise ApiError("words must be an array of strings")
        cleaned = [str(word).strip() for word in words if str(word).strip()]
        custom = custom_keywords_path()
        if not cleaned:
            custom.unlink(missing_ok=True)
            return {"words": [], "custom": False, "restart_required": True}
        try:
            written = write_keywords(custom, cleaned, model_paths()["kws_dir"])
        except KeywordError as exc:
            raise ApiError(str(exc)) from exc
        return {"words": written, "custom": True, "restart_required": True}

    # -------------------------------------------------------------------- turns

    def text(self, text: str) -> dict[str, Any]:
        """Run one turn from typed text, exactly as ``run_desktop.py`` does."""
        if not isinstance(text, str) or not text.strip():
            raise ApiError("text is required")
        result = self.runtime.say(text.strip())
        return {
            "route": result.route,
            "ok": bool(result.ok),
            "text": result.text or "",
            "reason": result.reason or "",
            "needs_confirmation": bool(result.needs_confirmation),
            "tool": result.tool or "",
            "elapsed_ms": result.elapsed_ms,
            "agents": list(result.agents or ()),
        }

    # ------------------------------------------------------------------ speaker

    def speaker_view(self) -> dict[str, Any]:
        """Enrollment status: names and counts. ``describe()`` is the only view.

        另外带上**要念的那几句**（`core/audio/enroll_prompts.py`）—— 脚本和页面共用同一份，
        所以两条路的提示不会各自漂移。页面据此决定画几个格子、每格写哪句话。
        """
        verifier = self._verifier()
        capture = self.stack.capture if self.stack is not None else None
        prompts = {
            "prompts": enroll_prompts_json(),
            "max_clips": DEFAULT_ROUNDS,
            "mic_running": bool(self.mic_running),
            # 注册模式：一个人都没注册时麦克风能开，但唤醒判定被永久按住。页面据此
            # 决定要不要显示「开麦克风」那颗按钮，以及要不要说「现在还不会响应唤醒词」。
            "enroll_only": bool(getattr(capture, "enroll_only", False)),
            "clips": len(self._enroll_clips),
        }
        if verifier is None:
            return {"available": False, "reason": "no verifier is attached", "speakers": [], **prompts}
        try:
            return {**verifier.describe(), **prompts}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": type(exc).__name__, "speakers": [], **prompts}

    def _verifier(self) -> Any:
        if self.stack is not None and getattr(self.stack, "verifier", None) is not None:
            return self.stack.verifier
        capture = getattr(self.runtime.plugin, "audio_capture", None)
        return getattr(capture, "verifier", None) if capture is not None else None

    def enroll(self, name: str, clips: Sequence[str]) -> dict[str, Any]:
        """Register a voice from base64 WAV clips recorded in the page.

        The audio exists as a decoded array for the length of this call and is
        never written anywhere. What persists is the embedding, in
        ``enrollment/``, which is gitignored biometric data.
        """
        name = (name or "").strip()
        if not name:
            raise ApiError("a speaker name is required")
        if not clips:
            raise ApiError("at least one recording is required")
        verifier = self._verifier()
        if verifier is None:
            raise ApiError("the voiceprint gate is not available on this host", status=409)

        chunks = []
        report = []
        for index, clip in enumerate(clips):
            try:
                samples = decode_wav_base64(clip)
            except AudioDecodeError as exc:
                raise ApiError(f"recording {index + 1}: {exc}") from exc
            measured = quality(samples)
            measured["index"] = index
            report.append(measured)
            chunks.append(samples)

        try:
            result = verifier.enroll(name, chunks)
        except Exception as exc:  # noqa: BLE001 - the message names the constraint
            raise ApiError(f"enrollment refused: {exc}", status=409) from exc
        finally:
            # Drop the references before returning, so the arrays are collectable
            # rather than kept alive by this frame for the response's lifetime.
            chunks.clear()

        return {
            "speaker": result.speaker,
            "samples_used": result.samples_used,
            "total_seconds": round(result.total_seconds, 3),
            "dim": result.dim,
            "quality": report,
            "audio_saved": False,
            "wake_armed": self._arm_wake_after_enrollment(),
        }

    def _arm_wake_after_enrollment(self) -> bool:
        """注册成功之后把注册模式解开。解开了返回 ``True``。

        判定在 `core/audio/capture.py` 的 `arm_after_enrollment()` 里（它复用声纹门自己的
        那条前提）。这里只负责在两条注册路径上都调它 —— 2026-09-01 实机漏的就是这一步：
        注册成功、页面显示成功、而 `wake_held` 仍然恒真，喊唤醒词没有任何反应。
        """
        capture = self.stack.capture if self.stack is not None else None
        armer = getattr(capture, "arm_after_enrollment", None)
        if not callable(armer):
            return False
        try:
            return bool(armer())
        except Exception:  # noqa: BLE001 - 解不开就维持按住（fail-closed 的那一侧）
            return False

    # -- 从采集缓冲注册（和校验同一条信道）--------------------------------------

    def capture_clip(self, seconds: float = 3.0) -> dict[str, Any]:
        """从**门读的那个环形缓冲**取一段，留在内存里等注册。

        阻塞 ``seconds`` 再取快照，所以页面上的交互和原来一样：点一下、说一句、拿到读数。
        取的是 `capture._ring`，也就是唤醒时送去校验的同一份音频 —— 「注册和校验同信道」
        因此是构造上成立的，不是靠谁记得选对设备。

        第一次注册**也走这条路**：一个人都没注册时 `capture.start()` 进注册模式（设备开着、
        唤醒判定被永久按住），所以这一页能把第一份声纹录完，不必先开终端跑脚本。
        """
        if len(self._enroll_clips) >= DEFAULT_ROUNDS:
            raise ApiError(f"已经录了 {DEFAULT_ROUNDS} 段，先注册或清空", status=409)
        samples, measured = self._snapshot(seconds, min_peak=LIVE_MIN_PEAK)
        self._enroll_clips.append(samples)
        return {"index": len(self._enroll_clips) - 1, "clips": len(self._enroll_clips), **measured}

    def _snapshot(self, seconds: float, *, min_peak: float) -> tuple[Any, dict[str, Any]]:
        """从环形缓冲取一段并量它。取样期间唤醒判定被按住。

        ``min_peak`` 是「原始峰值低到这个量级就拒绝」的那条线。注册和试一句用
        `LIVE_MIN_PEAK`；**校准输入音量时必须给 0** —— 偏轻正是它要修的东西，用同一条线
        拦住它等于「只有已经调好的机器才能校准」。
        """
        capture = self.stack.capture if self.stack is not None else None
        if capture is None or not callable(getattr(capture, "recent_audio", None)):
            raise ApiError("这台机器上没有采集缓冲，用 scripts/enroll_speaker.py 注册", status=409)
        if not self.mic_running:
            raise ApiError(
                "麦克风没在跑。在这一页点「开麦克风」，或者用 `--voice` 启动控制台",
                status=409,
            )
        # 聆听期间音频**全部喂给识别器、一个样本都不进环形缓冲**，那时取快照只会拿到一段
        # 空的（然后质量门判「太轻」，分数 0）。拒绝比给一个假读数好。
        if getattr(capture, "listening", False):
            raise ApiError("正在聆听刚才那次唤醒，等这一轮说完再录", status=409)
        span = max(0.5, min(float(seconds or 3.0), float(getattr(capture, "buffer_seconds", 3.0))))
        # **取样期间把唤醒判定按住。** 页面提示让人说的就是唤醒词，真命中的话
        # `_authorise` 的 finally 会把刚录的缓冲清掉、并切进聆听模式（之后的块不再入缓冲），
        # 于是这一段固定是空的 —— 使用者 2026-08-31 报的「试一句经常相似度为 0」就是它。
        holder = getattr(capture, "hold_wake_for", None)
        if callable(holder):
            holder(span + 0.5)
        capture.forget_recent_audio()
        time.sleep(span + 0.15)  # 一点余量，让最后那一块音频也进来
        samples = capture.recent_audio(span)
        measured = quality(samples)
        # **有没有人说话，由 VAD 判，不由峰值判。**
        #
        # 2026-08-31 实机：使用者什么都没说、等了一会，试一句报「相似度 0.979 通过」。
        # 数字是真算出来的，但两边都是**放大后的房间底噪**。峰值、RMS、削波比例都是能量
        # 统计量，而「是不是人声」不是能量问题 —— 用能量去近似它必然在某台设备上翻车。
        # 实测（core/audio/vad.py 的冒烟）：同一段底噪放大 10 倍后 VAD 判 False，而真实
        # 人声缩到峰值 **0.01** 仍然判 True。那才是「无论何种设备、音量」要的判据。
        #
        # 峰值那条线仍然留着，但降级成**第二道**：VAD 缺模型时它是唯一的保险。
        if not capture.has_speech(samples):
            raise ApiError(
                f"这 {span:.0f} 秒里没有检测到人说话（VAD 判定）"
                f"，峰值 {measured['peak']:.4f} / rms {measured['rms']:.4f}。"
                "对着麦克风念格子里那一句再点一次",
                status=409,
            )
        if measured["peak"] < min_peak:
            raise ApiError(
                f"设备原始峰值只有 {measured['peak']:.4f}（rms {measured['rms']:.4f}），"
                f"低于 {min_peak} —— 这个量级和「麦克风没在收音」区分不开，"
                "任何相似度都没有意义。先点「校准输入音量」（它会直接改 Windows 那一侧的"
                "输入音量并复测），再回来录",
                status=409,
            )
        return samples, measured

    def clear_clips(self) -> dict[str, Any]:
        self._enroll_clips.clear()
        return {"clips": 0}

    def enroll_captured(self, name: str) -> dict[str, Any]:
        """用缓冲里那几段注册。音频在这次调用之后就没了 —— 留下的只有向量。"""
        name = (name or "").strip()
        if not name:
            raise ApiError("a speaker name is required")
        if not self._enroll_clips:
            raise ApiError("还没有录音段，先点「录一段」")
        verifier = self._verifier()
        if verifier is None:
            raise ApiError("the voiceprint gate is not available on this host", status=409)
        report = [{"index": index, **quality(chunk)} for index, chunk in enumerate(self._enroll_clips)]
        try:
            result = verifier.enroll(name, list(self._enroll_clips))
        except Exception as exc:  # noqa: BLE001 - the message names the constraint
            raise ApiError(f"enrollment refused: {exc}", status=409) from exc
        finally:
            self._enroll_clips.clear()
        return {
            "speaker": result.speaker,
            "samples_used": result.samples_used,
            "total_seconds": round(result.total_seconds, 3),
            "dim": result.dim,
            "quality": report,
            "audio_saved": False,
            # 注册模式**在这里**解开：麦克风已经在跑，不解开就得重启才能唤醒。
            "wake_armed": self._arm_wake_after_enrollment(),
        }

    def verify_captured(self, seconds: float = 3.0) -> dict[str, Any]:
        """「试一句」：录一段，取**门实际用的那个窗长**过一次校验，把相似度报出来。

        **窗长必须和门一致，这是 2026-08-30 查出来的一条谎。** 唤醒时送去校验的是
        「命中前 `verify_seconds`（默认 1.5）秒」，而这里此前拿整段 3 秒去算 —— 实测同一个
        档案用 1.0 s 的窗得 0.774、用 3.0 s 的窗得 0.846，也就是这个诊断会比门实际给的分
        **高 0.07 以上**；叠上「实机那个窗里有一截静音」（实测再掉 0.05–0.09），使用者看到
        的差距就是 0.2。**一个报数比现实好的诊断比没有诊断更糟**，因为它会让人去查别处。

        取的是**尾部**那一段：唤醒时那个窗口是「以唤醒词结尾」的，所以对着这个按钮说
        「你好小沃」再停下，取尾部才是同一个形状。
        """
        verifier = self._verifier()
        if verifier is None:
            raise ApiError("the voiceprint gate is not available on this host", status=409)
        # **没人注册就先说清楚，别先录 3 秒再报一个 0。**
        #
        # 2026-09-01 实机：使用者说「试一句里『你好小沃』的相似度为 0」。那个 0 是对的
        # —— `verify()` 在没有档案时返回 `(False, None, 0.0, "no speaker enrolled")` ——
        # 但页面把它显示成一个分数，读起来像「你的声音不像你」。**一个能被误读成测量结果的
        # 常量比没有读数更糟**：它会让人去查麦克风、查距离、查阈值，而真正的原因是
        # 档案表是空的。
        if not list(getattr(verifier, "speakers", ()) or ()):
            raise ApiError(
                "还没有人注册 —— 试一句没有可比对的档案，相似度必然是 0（不是「不像你」）。"
                "先录上面那几句、填个名字、点「注册」",
                status=409,
            )
        before = list(self._enroll_clips)
        try:
            self.capture_clip(seconds)
            samples = self._enroll_clips[-1]
        finally:
            # 试一句不该占用注册的那三格。
            self._enroll_clips[:] = before
        capture = self.stack.capture if self.stack is not None else None
        window_s = float(getattr(capture, "verify_seconds", 1.5) or 1.5)
        wanted = int(window_s * 16000)
        window = samples[-wanted:] if len(samples) > wanted else samples
        started = time.perf_counter()
        try:
            # **throttle=False**：这一次校验不进暴力防护。一次本机、已鉴权、由人点出来的
            # 诊断不是暴力尝试 —— 而 2026-08-31 实机里它正是：连点几下试一句就把真实唤醒门
            # 推进了 30 秒冷却，日志上是「声纹拒绝：cooling down for 25.4s」，使用者看到的
            # 是「说了唤醒词但根本没检测到」。
            result = verifier.verify(window, sample_rate=16000, throttle=False)
        except Exception as exc:  # noqa: BLE001 - a fault is a rejection
            return {
                "accepted": False,
                "speaker": None,
                "score": 0.0,
                "reason": f"verifier error: {type(exc).__name__}",
                "window_s": round(len(window) / 16000, 2),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                **quality(window),
            }
        return {
            "accepted": bool(result.accepted),
            "speaker": result.speaker,
            "score": round(float(result.score), 4),
            "reason": result.reason,
            "threshold": getattr(verifier, "threshold", None),
            # 报出用了多长的窗：这个数字和门用的必须相同，而「相同」这件事要看得见。
            "window_s": round(len(window) / 16000, 2),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **quality(window),
        }

    def remove_speaker(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ApiError("a speaker name is required")
        verifier = self._verifier()
        if verifier is None:
            raise ApiError("the voiceprint gate is not available on this host", status=409)
        try:
            existed = verifier.remove(name)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"could not remove: {type(exc).__name__}") from exc
        return {"removed": bool(existed), "speakers": self.speaker_view().get("speakers", [])}

    # -- 输入设备与 OS 那一侧的输入音量 ------------------------------------------

    def _device_in_use(self) -> tuple[int | str | None, str]:
        """当前在用的输入设备：`sounddevice` 的选择子，和它的**名字**。

        名字才是能对上 Windows 那一侧的东西 —— 而索引会漂：`input.device = "2"` 在
        2026-08-29 是「耳机 (沉麟的耳机)」，2026-09-01 实测已经变成「麦克风阵列
        (Realtek(R) Audio)」，因为中间插拔过设备。一个只报索引的界面会让这件事永远不可见。
        """
        capture = self.stack.capture if self.stack is not None else None
        selector = getattr(capture, "device", None)
        if selector is None:
            config = getattr(self.stack, "config", None)
            if isinstance(config, Mapping):
                selector = resolve_device(dict(config))
        try:
            return selector, winlevel.device_name(selector)
        except Exception:  # noqa: BLE001 - 名字取不到不该让整个 /api/state 挂掉
            return selector, ""

    def input_devices(self) -> dict[str, Any]:
        """输入设备清单，**带上 Windows 那一侧的输入音量和静音状态**。

        为什么要合这两份：一台机器上同一个物理麦克风会以 MME / DirectSound / WASAPI /
        WDM-KS 四个条目出现（所以 `input.device` 填索引），而「这只麦克风能不能用」取决于
        端点那一侧的音量和静音 —— 2026-09-01 实测同一时刻「耳机」是 0.01、「麦克风阵列」
        是 0.82。这两个数字此前在界面上完全不存在，于是「设备坏了」和「音量是 1%」长得
        一模一样。
        """
        selector, in_use = self._device_in_use()
        rows: list[dict[str, Any]] = []
        try:
            import sounddevice as sd  # type: ignore

            apis = sd.query_hostapis()
            for index, device in enumerate(sd.query_devices()):
                if device["max_input_channels"] <= 0:
                    continue
                rows.append({
                    "index": index,
                    "name": str(device["name"]),
                    "api": str(apis[device["hostapi"]]["name"]),
                    "channels": int(device["max_input_channels"]),
                    "in_use": str(index) == str(selector) or str(device["name"]) == in_use,
                })
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "devices": []}

        levels: dict[str, dict[str, Any]] = {}
        reason = ""
        try:
            levels = {end.name: end.describe() for end in winlevel.endpoints()}
        except Exception as exc:  # noqa: BLE001 - 没有音量控制不影响列设备
            reason = str(exc)
        for row in rows:
            row["os"] = levels.get(row["name"])
        return {
            "available": True,
            "reason": reason,
            "in_use": in_use,
            "selector": selector if selector is None else str(selector),
            "devices": rows,
        }

    def set_input_level(self, level: float, device: str = "") -> dict[str, Any]:
        """直接改 Windows 那一侧的输入音量（0.0–1.0），顺带取消静音。

        这是「不该由人去试」的那件事的一半 —— 另一半是 `calibrate_input()`，它自己决定
        该设多少。留一个手动入口，因为知道自己要什么的人不该被闭环挡住。
        """
        name = (device or "").strip() or self._device_in_use()[1]
        if not name:
            raise ApiError("认不出在用哪只输入设备（拿不到设备名）", status=409)
        try:
            end = winlevel.set_level(name, float(level))
        except winlevel.LevelUnavailable as exc:
            raise ApiError(str(exc), status=409) from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"{type(exc).__name__}: {exc}", status=409) from exc
        return {"device": end.name, **end.describe()}

    def calibrate_input(self, seconds: float = CALIBRATE_SECONDS) -> dict[str, Any]:
        """**闭环**校准输入音量：量说话时的峰值 → 改 OS 那一侧的音量 → 复测。

        使用者三次提出同一件事：「真正的最佳效果应该是无论何种设备、音量，都能准确的识别
        唤醒词」。此前我们只会**建议**他去调 Windows 的滑条，而实测那个可用窗口很窄、位置
        又取决于用哪只麦克风、戴不戴耳机、离多远 —— 一个成熟的产品不该把这件事留给人。

        为什么是闭环而不是「设成某个默认值」：合适的音量取决于麦克风的灵敏度。同一时刻
        实测「耳机」在 0.01、「麦克风阵列」在 0.82，而两者都是这台机器上的正常状态 ——
        没有哪个常数对两只麦克风同时成立。唯一可靠的判据是**量出来的说话峰值**。

        **算法是二分，不是按比例缩放。** ``SetMasterVolumeLevelScalar`` 的标度不是幅度的
        线性函数：它走的是 dB 曲线，而曲线的范围各家驱动不一样（实测这台机器上 0.01 和
        0.82 分别对应约 0.03× 和 0.54× 的幅度）。按比例算下一步要先假设一个曲线，猜错就
        来回过冲。二分只依赖一件必然成立的事 —— 音量调高，峰值不会变小 —— 所以它对任何
        曲线都收敛，4 轮把标度收敛到 1/16。

        为什么不在开麦时自动跑：它要求人**一直在说话**。没有语音时唯一诚实的动作是什么都
        不做 —— 拿房间底噪去校准会把音量推到顶，那正好是削波那一端。
        """
        name = self._device_in_use()[1]
        if not name:
            raise ApiError("认不出在用哪只输入设备（拿不到设备名）", status=409)
        # 麦克风没开就**立刻**拒绝，而不是走满 8 轮然后报「没听到说话」：那个提示会让人以为
        # 自己说得不够大声，而真正的原因是设备根本没开。
        if not self.mic_running:
            raise ApiError("麦克风没在跑。先点「开麦克风」—— 校准要量你说话时的峰值", status=409)
        try:
            before = winlevel.read_level(name)
        except winlevel.LevelUnavailable as exc:
            raise ApiError(f"{exc}（这台机器上没法自动调，只能用系统的声音设置）", status=409)
        low, high = CALIBRATE_BAND
        span = max(1.0, min(float(seconds or CALIBRATE_SECONDS), 3.0))
        level = before.level
        lo, hi = 0.0, 1.0
        trail: list[dict[str, Any]] = []
        heard = 0
        settled = False
        for _attempt in range(2 * CALIBRATE_ROUNDS):
            if heard >= CALIBRATE_ROUNDS:
                break
            try:
                _samples, measured = self._snapshot(span, min_peak=0.0)
            except ApiError as exc:
                # 没听到说话：记一笔继续等，**不动音量**。改了就等于拿底噪校准。
                trail.append({"level": round(level, 4), "silent": True, "why": str(exc)[:60]})
                continue
            heard += 1
            peak = float(measured["peak"])
            trail.append({
                "level": round(level, 4),
                "peak": round(peak, 4),
                "rms": round(float(measured["rms"]), 4),
                "clip_ratio": round(float(measured["clip_ratio"]), 4),
            })
            if low <= peak <= high:
                settled = True
                break
            if peak < low:
                lo, wanted = level, (level + hi) / 2.0
            else:
                hi, wanted = level, (lo + level) / 2.0
            if abs(wanted - level) < 0.01:
                # 区间已经收拢到比可调精度还小 —— 再走一步只是重复上一次，如实停下。
                break
            try:
                level = winlevel.set_level(name, wanted).level
            except winlevel.LevelUnavailable as exc:
                raise ApiError(str(exc), status=409) from exc
        after = winlevel.read_level(name)
        return {
            "device": name,
            "before": before.describe(),
            "after": after.describe(),
            "target": CALIBRATE_TARGET,
            "band": [low, high],
            "trail": trail,
            "heard": heard,
            "settled": settled,
            "hint": (
                ""
                if settled
                else "没听到说话 —— 校准要你连续念一段话（念注册页那几句就行），再点一次"
                if heard == 0
                else "调到了可调范围的边上还没进目标带：这只麦克风的灵敏度不够（或者离得太远），"
                "换一只输入设备或靠近些再试"
            ),
        }


    def memory(self, query: str = "") -> dict[str, Any]:
        """Mid-term facts only -- never conversation turns.

        Facts are already human-readable in ``memory/facts/*.md``, so showing them
        adds no exposure. Turns are the transcript of everything said, and there is
        no question the console answers by displaying it.
        """
        recaller = getattr(self.runtime, "memory_recaller", None)
        if recaller is None:
            return {"attached": False, "facts": []}
        try:
            if query.strip():
                records = recaller.facts(query.strip())
            else:
                # No query means "show me what is in there". ``recall`` with an
                # empty string has no terms to match, so list instead of search.
                records = recaller.store.list_records(scope="mid", kind="fact", limit=50)
        except Exception as exc:  # noqa: BLE001
            return {"attached": True, "error": type(exc).__name__, "facts": []}
        facts = [
            {
                "id": record.id,
                "kind": record.kind,
                "text": record.text,
                "tags": list(record.tags or ()),
            }
            for record in records
            if getattr(record, "scope", "mid") == "mid"
        ]
        return {"attached": True, "facts": facts}

    # ---------------------------------------------------------------- microphone

    def mic_start(self) -> dict[str, Any]:
        """Open the capture device and start driving turns from speech.

        The pump loop is the caller's job (``scripts/run_console.py`` runs it on a
        worker thread): this only opens the device. Running turns on the audio
        callback would hold it for a whole dispatch plus playback.
        """
        if self.stack is None or self.stack.capture is None:
            raise ApiError("no voice stack is attached to this console", status=409)
        if self.mic_running:
            return {"running": True, "already": True}
        self.runtime.attach_microphone(self.stack.capture)
        try:
            self.stack.capture.start()
        except Exception as exc:  # noqa: BLE001 - a refused gate lands here too
            raise ApiError(f"{type(exc).__name__}: {exc}", status=409) from exc
        self.mic_running = True
        return {
            "running": True,
            # 一个人都没注册时开的是**注册模式**：设备开着、缓冲照常填，但唤醒判定被永久
            # 按住（`capture.enroll_only`）。页面据此把「录一段」放出来，同时说清楚现在
            # 还不会响应唤醒词。见 capture._check_gate_preconditions 的注释。
            "enroll_only": bool(getattr(self.stack.capture, "enroll_only", False)),
        }

    def mic_stop(self) -> dict[str, Any]:
        if self.stack is None or self.stack.capture is None:
            return {"running": False}
        try:
            self.stack.capture.stop()
        finally:
            self.mic_running = False
        return {"running": False}

    # -------------------------------------------------------------- model tests

    def test_tts(self, text: str, *, play: bool = True) -> dict[str, Any]:
        """Synthesize one phrase, optionally through the speakers.

        ``play=False`` measures the model without needing an output device, which
        is the difference between "does TTS work" and "can I hear it" -- two
        questions with two different answers on a machine with no speakers.
        """
        text = (text or "").strip() or "唤醒球已就绪。"
        provider = self._require_provider("tts")
        started = time.perf_counter()
        try:
            audio = provider.speak(text) if play else provider.synthesize(text)
        except Exception as exc:  # noqa: BLE001 - the message names the constraint
            raise ApiError(f"tts failed: {type(exc).__name__}: {exc}", status=409) from exc
        return {
            "text": text,
            "played": bool(play),
            "sample_rate": audio.sample_rate,
            "samples": int(getattr(audio.samples, "size", 0)),
            "seconds": round(int(getattr(audio.samples, "size", 0)) / max(1, audio.sample_rate), 3),
            "synth_ms": audio.elapsed_ms,
            "total_ms": int((time.perf_counter() - started) * 1000),
        }

    def test_asr(self, clip: str) -> dict[str, Any]:
        """Transcribe one recorded clip with the real streaming recognizer."""
        samples = self._decode(clip, "recording")
        provider = self._require_provider("asr")
        started = time.perf_counter()
        try:
            stream = provider.create_stream()
            # Feed in 100 ms blocks, the same size the capture callback uses, so
            # this measures the model on the shape it will actually see.
            step = 1600
            endpoints = 0
            for offset in range(0, samples.size, step):
                result = provider.feed(stream, samples[offset : offset + step], 16000)
                if result.is_endpoint:
                    endpoints += 1
            text = provider.finalize(stream)
            provider.reset(stream)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"asr failed: {type(exc).__name__}: {exc}", status=409) from exc
        return {
            "text": text,
            "endpoints": endpoints,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **quality(samples),
        }

    def test_kws(self, clip: str) -> dict[str, Any]:
        """Run one clip past the keyword spotter and report the hits.

        The score is absent by design: sherpa-onnx 1.13.4's ``KeywordResult``
        carries no per-hit confidence, so reporting a number here would be
        inventing one. The number that reaches ``wake.detected`` is the voiceprint
        similarity, which is measured.
        """
        samples = self._decode(clip, "recording")
        provider = self._require_provider("kws")
        try:
            stream = provider.create_stream()
            hits: list[str] = []
            for offset in range(0, samples.size, 1600):
                for keyword, _score in provider.feed(
                    stream, samples[offset : offset + 1600], 16000
                ):
                    hits.append(keyword)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"kws failed: {type(exc).__name__}: {exc}", status=409) from exc
        return {"hits": hits, "hit": bool(hits), "score": None, **quality(samples)}

    def test_speaker(self, clip: str) -> dict[str, Any]:
        """Verify one clip against the enrolled voiceprints.

        Returns the decision, the similarity and the reason -- never a vector, and
        never the enrollment data itself. A rejection here is the gate working.
        """
        samples = self._decode(clip, "recording")
        verifier = self._verifier()
        if verifier is None:
            raise ApiError("the voiceprint gate is not available on this host", status=409)
        started = time.perf_counter()
        try:
            result = verifier.verify(samples, sample_rate=16000)
        except Exception as exc:  # noqa: BLE001 - a fault is a rejection
            return {
                "accepted": False,
                "speaker": None,
                "score": 0.0,
                "reason": f"verifier error: {type(exc).__name__}",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                **quality(samples),
            }
        return {
            "accepted": bool(result.accepted),
            "speaker": result.speaker,
            "score": round(float(result.score), 4),
            "reason": result.reason,
            "threshold": getattr(verifier, "threshold", None),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            **quality(samples),
        }

    def test_agent(self, name: str, text: str) -> dict[str, Any]:
        """Run one real turn against one configured agent.

        This is the REAL-AGENT probe with a button on it. It reports what actually
        came back, including the failure -- an agent that is not logged in answers
        with an error chunk, and that is a result worth seeing rather than hiding.
        """
        name = (name or "").strip()
        adapters = getattr(self.runtime, "adapters", {})
        adapter = adapters.get(name)
        if adapter is None:
            raise ApiError(f"no such agent: {name or '(unnamed)'}", status=404)
        from core.agents.contract import Task

        task = Task(id=f"console-{int(time.time())}", text=(text or "你好").strip())
        started = time.perf_counter()
        chunks: list[dict[str, Any]] = []
        answer: list[str] = []
        error = ""
        try:
            # 契约里的方法是 `stream`,不是 `run` —— 这里此前写的是 `adapter.run(task)`,
            # 于是每次点「试跑」都换回 `AttributeError: 'CliAgentAdapter' object has no
            # attribute 'run'`,而外面那层 except 把它记成「受阻」并显示「claude 没答」。
            # 一个把自己的拼写错误报成「agent 受阻」的探针比没有探针更糟:它指向了错的地方。
            for chunk in adapter.stream(task):
                chunks.append({"kind": chunk.kind, "chars": len(chunk.text or "")})
                if chunk.text:
                    answer.append(chunk.text)
                if chunk.kind == "done":
                    error = chunk.error or ""
                    break
        except Exception as exc:  # noqa: BLE001 - an adapter may still raise on setup
            error = f"{type(exc).__name__}: {exc}"
        joined = "".join(answer)
        return {
            "agent": name,
            "ok": not error,
            "error": error,
            # Capped: this is a probe, not a chat window.
            "text": joined[:2000],
            "chars": len(joined),
            "chunks": len(chunks),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "level": "REAL-AGENT" if not error else "attempted",
        }

    def test_tool(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Run one tool through the same gate the voice path uses.

        No shortcut and no separate policy: a tool that would be refused for a
        spoken request is refused here too, and ``shell.run`` comes back as
        ``needs_confirmation`` because this console does not confirm it.
        """
        runner = getattr(self.runtime, "tool_runner", None)
        if runner is None:
            raise ApiError("no tool runner is attached", status=409)
        from core.tools.contract import ToolRequest

        request = ToolRequest(
            tool=(tool or "").strip(),
            arguments=dict(arguments or {}),
            origin="voice",
            speaker=self.runtime.effective_speaker,
        )
        result = runner.run(request)
        return {
            "tool": result.tool,
            "ok": bool(result.ok),
            "output": (result.output or "")[:4000],
            "error": result.error or "",
            "needs_confirmation": bool(result.needs_confirmation),
            "confirm_where": "orb" if result.needs_confirmation else "",
            "audit": {k: v for k, v in (result.audit or {}).items() if k != "command"},
        }

    # -- test helpers ---------------------------------------------------------

    def _decode(self, clip: str, label: str) -> np.ndarray:
        try:
            return decode_wav_base64(clip)
        except AudioDecodeError as exc:
            raise ApiError(f"{label}: {exc}") from exc

    def _require_provider(self, which: str) -> Any:
        """One of the stack's providers, loaded, or a 409 that says which is missing."""
        provider = getattr(self.stack, which, None) if self.stack is not None else None
        if provider is None:
            raise ApiError(f"{which} is not available (disabled or model missing)", status=409)
        status = provider.load()
        if not status.available:
            raise ApiError(
                f"{which} did not load: {status.details.get('reason', 'unknown')}", status=409
            )
        return provider

    # ----------------------------------------------------------- agent registry

    def agents_config(self) -> dict[str, Any]:
        """One block per ``[[agents]]`` entry, split into settings and facts.

        ``locked`` keys are shown but not editable. Hiding them would be worse: an
        operator looking for "why is this agent running that command" needs to see
        the command, and the answer to "why can't I change it here" is on the page
        rather than in a docstring.
        """
        path = self.config_dir / "agents.toml"
        if not path.is_file():
            return {"file": "agents.toml", "present": False, "entries": []}
        try:
            found = scan(path)
        except Exception as exc:  # noqa: BLE001
            return {"file": "agents.toml", "present": True, "error": type(exc).__name__, "entries": []}
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in found.items():
            section = entry["section"]
            block = entries.setdefault(section, {"section": section, "keys": []})
            if entry["name"] == "name":
                block["name"] = entry["value"]
            if entry["name"] == "kind":
                block["kind"] = entry["value"]
            block["keys"].append(
                {
                    "key": key,
                    "name": entry["name"],
                    "value": entry["value"],
                    "type": type(entry["value"]).__name__,
                    "editable": entry["editable"] and entry["name"] in AGENT_EDITABLE,
                    "locked": entry["name"] not in AGENT_EDITABLE,
                }
            )
        return {
            "file": "agents.toml",
            "present": True,
            "entries": [entries[key] for key in sorted(entries, key=_section_order)],
            "editable_keys": sorted(AGENT_EDITABLE),
        }

    def agents_update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Write per-entry settings. Anything outside ``AGENT_EDITABLE`` is refused.

        Validation runs the registry's own schema checker, so an entry that would
        fail at startup fails here instead -- with the same message.
        """
        rejected = sorted(key for key in updates if not _agent_key_allowed(key))
        if rejected:
            raise ApiError(
                "these agent keys are not editable from the console (edit the file): "
                + ", ".join(rejected),
                status=403,
            )
        if not updates:
            return {"changed": {}}
        path = self.config_dir / "agents.toml"

        def validate(candidate: Path) -> None:
            from core.agents.registry import load_agents_config

            load_agents_config(candidate)

        try:
            changed = set_scalars(path, updates, validate=validate)
        except ConfigEditError as exc:
            raise ApiError(str(exc)) from exc
        return {"changed": changed, "restart_required": True}

    # ------------------------------------------------------------------ profile

    def _facts_dir(self) -> Path:
        from core.memory.store import load_memory_config

        return Path(load_memory_config()["facts_dir"])

    def _fact_path(self, name: str) -> Path:
        """Resolve a facts file name, refusing anything that could escape the dir.

        Two checks, not one: the pattern rejects separators and ``..`` up front, and
        the resolved path is confirmed to still be inside the directory. The second
        catches what a symlink could do that the first cannot see.
        """
        name = (name or "").strip()
        if not _FACT_NAME.match(name):
            raise ApiError("a profile file must be a plain *.md name with no path")
        root = self._facts_dir().resolve()
        candidate = (root / name).resolve()
        if candidate != root and root not in candidate.parents:
            raise ApiError("that path is outside the profile directory", status=403)
        return candidate

    def profile_list(self) -> dict[str, Any]:
        """The Markdown files that *are* the mid-term facts.

        The files are the source of truth and SQLite is an index over them (ADR
        004), so the console edits files and then folds them back in. Editing rows
        in the database would put the two out of step in the direction that loses
        the hand-written version.
        """
        root = self._facts_dir()
        if not root.is_dir():
            return {"dir": str(root), "present": False, "files": []}
        files = []
        for path in sorted(root.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                files.append({"file": path.name, "error": type(exc).__name__})
                continue
            title = _fact_title(text)
            files.append(
                {
                    "file": path.name,
                    "title": title[:120],
                    "bytes": len(text.encode("utf-8")),
                    "lines": text.count("\n") + 1,
                }
            )
        return {"dir": str(root), "present": True, "files": files}

    def profile_read(self, name: str) -> dict[str, Any]:
        path = self._fact_path(name)
        if not path.is_file():
            raise ApiError(f"no such profile file: {name}", status=404)
        return {"file": path.name, "text": path.read_text(encoding="utf-8")}

    def profile_save(self, name: str, text: str) -> dict[str, Any]:
        """Write one profile file, refusing credential-shaped content.

        The refusal reuses ``looks_like_secret`` -- the same filter the memory
        writer applies -- so a private key pasted into the profile editor is
        rejected whole rather than masked. Masking a multi-line key is exactly the
        case that leaves the body on disk.
        """
        from core.memory.write import looks_like_secret

        path = self._fact_path(name)
        if not isinstance(text, str) or not text.strip():
            raise ApiError("a profile file cannot be empty (delete it instead)")
        if looks_like_secret(text):
            raise ApiError(
                "that text looks like a credential; the memory layer refuses those whole",
                status=403,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_suffix(".md.tmp")
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, path)
        return {"file": path.name, "bytes": len(text.encode("utf-8")), **self.profile_sync()}

    def profile_delete(self, name: str) -> dict[str, Any]:
        path = self._fact_path(name)
        existed = path.is_file()
        if existed:
            path.unlink()
        # ``prune=True`` is what makes the deletion reach the index. It is safe
        # here because the directory demonstrably exists -- we just read a file
        # out of it -- which is the condition the default guards against.
        return {"deleted": existed, **self.profile_sync(prune=existed)}

    def profile_sync(self, *, prune: bool = False) -> dict[str, Any]:
        """Fold the files back into the index. The files win."""
        writer = getattr(self.runtime, "memory_writer", None)
        if writer is None:
            return {"synced": False}
        try:
            counts = writer.sync_facts(prune=prune)
        except Exception as exc:  # noqa: BLE001
            return {"synced": False, "error": type(exc).__name__}
        return {"synced": True, "counts": counts}

    # ---------------------------------------------------------------------- mcp

    def mcp_view(self) -> dict[str, Any]:
        """MCP servers: what is configured, what is running, what is locked.

        Locked keys are shown. Somebody looking at "why does this server expose
        only two tools" needs to see the ``allow`` list, and the answer to "why
        can't I edit it here" belongs on the page rather than in a docstring.
        """
        path = self.config_dir / "mcp.toml"
        payload: dict[str, Any] = {"file": "mcp.toml", "present": path.is_file()}
        if path.is_file():
            try:
                found = scan(path)
            except Exception as exc:  # noqa: BLE001
                found = {}
                payload["error"] = type(exc).__name__
            globals_: list[dict[str, Any]] = []
            servers: dict[str, dict[str, Any]] = {}
            for key, entry in found.items():
                shaped = {
                    "key": key,
                    "name": entry["name"],
                    "value": entry["value"],
                    "type": type(entry["value"]).__name__,
                    "editable": entry["editable"] and _mcp_key_allowed(key),
                    "locked": not _mcp_key_allowed(key),
                }
                if entry["section"] == "mcp":
                    globals_.append(shaped)
                elif entry["section"].startswith("servers["):
                    block = servers.setdefault(
                        entry["section"], {"section": entry["section"], "keys": []}
                    )
                    if entry["name"] == "name":
                        block["name"] = entry["value"]
                    block["keys"].append(shaped)
            payload["settings"] = globals_
            payload["servers"] = [servers[key] for key in sorted(servers, key=_section_order)]
        runner = getattr(self.runtime, "tool_runner", None)
        registry = getattr(runner, "mcp", None) if runner is not None else None
        payload["runtime"] = registry.describe() if registry is not None else None
        payload["tools"] = sorted(
            name for name in getattr(runner, "tools", {}) if str(name).startswith("mcp.")
        )
        return payload

    def mcp_update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Flip the master switch or a server's own switch. Nothing else."""
        rejected = sorted(key for key in updates if not _mcp_key_allowed(key))
        if rejected:
            raise ApiError(
                "these MCP keys are not editable from the console (edit the file): "
                + ", ".join(rejected),
                status=403,
            )
        if not updates:
            return {"changed": {}}

        def validate(candidate: Path) -> None:
            from core.tools.mcp import load_mcp_config

            load_mcp_config(candidate)

        try:
            changed = set_scalars(self.config_dir / "mcp.toml", updates, validate=validate)
        except ConfigEditError as exc:
            raise ApiError(str(exc)) from exc
        return {"changed": changed, "restart_required": True}


__all__ = [
    "AGENT_EDITABLE",
    "EDITABLE",
    "MCP_EDITABLE",
    "MCP_SERVER_EDITABLE",
    "ApiError",
    "ConsoleApi",
]
