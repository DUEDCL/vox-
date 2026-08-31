"""Voice stack configuration: parameters from TOML, paths from the environment.

The split is the same one ``load_speaker_config`` states and for the same reason:
a config file that is checked into a repository must not record one machine's
disk layout. So ``config/voice.toml`` carries thresholds, thread counts and
on/off switches, while the four model locations come from environment variables
with ``models/`` defaults.

Unknown keys **raise**. That is the stricter of the two stances already present in
this project (``load_speaker_config`` ignores them, ``load_tools_config`` refuses
them), and it is the right one here for the same reason it was right for tools: a
misspelled ``keywords_threshold`` silently keeps the default while the operator
believes it was changed, and a setting that looks applied but is not is worse than
no setting at all.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from core.audio.acks import DEFAULT_ACKS

DEFAULT_CONFIG_NAME = "voice.toml"

#: Pinned model directory names under ``models/`` (ADR 001). Overridable per
#: machine through the environment variable next to each one.
DEFAULT_KWS_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
#: 默认的流式 ASR 模型。
#:
#: 2026-08-29 换掉了 ``zh-14M-2023-02-23``，因为它在本机四段真人录音上的字错误率是
#: **21.4%**，而其中一句「检查目前运行状态是否正常」被听成「起床先生信息的三个情况」——
#: 转写错到那个程度，后面整条链路（意图、派发、回答）都在回答一个没人问过的问题。
#:
#: 同一批录音上的实测（人工写的参考文本，编辑距离按字算）：
#:
#: | 模型 | 平均 CER | RTF | 那句长句 |
#: |---|---|---|---|
#: | zh-14M-2023-02-23 | 21.4% | 0.014 | 18.8% |
#: | **multi-zh-hans-2023-12-12** | **14.1%** | **0.061** | **6.2%（完全正确）** |
#: | zh-int8-2025-06-30 | 16.1% | 0.095 | 6.2% |
#:
#: 选中间那个：比 2025 版**又准又快**（官方不公布 CER，只有实测能看出这一点）。RTF 0.061
#: 是实时的 16 倍速，常驻负载完全吃得下。剩下的错误几乎全在「沃」这个字上（听成「我/窝/
#: 吴」），而那不影响可用性 —— 唤醒靠 KWS 不靠 ASR，唤醒词那一块音频本来就不进识别器。
DEFAULT_ASR_DIR = "sherpa-onnx-streaming-zipformer-multi-zh-hans-2023-12-12"
DEFAULT_TTS_DIR = "vits-melo-tts-zh_en"
DEFAULT_VAD_MODEL = "silero_vad.onnx"

#: 自定义唤醒词表的约定位置（相对仓库根）。``wake.keywords_file`` 留空且这个文件存在
#: 时就用它；两者都没有才回落到模型自带的 ``keywords.txt``。控制台的「唤醒词」那一栏
#: 写的就是这个文件，手改它等效。
DEFAULT_KEYWORDS_FILE = "config/keywords.txt"

#: 唤醒确认音的缓存目录（相对仓库根，gitignored）。文件是从配置里那行文本派生的，
#: 所以它是产物不是资源。
ACK_CACHE_DIR = ".vox/acks"

#: One entry per ``[section]`` the file may contain, mapping key -> default. The
#: shape doubles as the validator: anything outside it is a typo.
_SCHEMA: dict[str, dict[str, Any]] = {
    "wake": {
        "keywords_file": "",
        "keywords_threshold": 0.25,
        "num_threads": 2,
        # 唤醒确认音。命中之后先应一声再开始听 —— 没有这一声，人会以为没听见而重复喊，
        # 而重复的第二遍会落进已经开着的识别器。空字符串 = 关掉。分隔符收「，,；;|、」。
        "acks": DEFAULT_ACKS,
    },
    "asr": {"enabled": True, "num_threads": 2},
    # provider 选本机还是云端。**这是 2026-08-29 新加的四个键** —— 在那之前 TTS 只有
    # 本机 sherpa 一条路，所以「在控制台把合成换成 cosyvoice、音色 longyuan」在任何一层
    # 都做不到（见 core/audio/tts_cloud.py 模块头列的三条原因）。
    #
    # provider = "sherpa"     读 tts_dir 下的本机 VITS 模型，speaker_id / speed 生效
    # provider = "dashscope"  走阿里云百炼非实时 HTTP 合成，model / voice / key_env 生效
    #
    # key_env 只写**变量名**，值一律从环境变量读 —— 与 agents.toml 同一条规矩。
    "tts": {
        "enabled": True,
        "provider": "sherpa",
        "speaker_id": 0,
        "speed": 1.0,
        "num_threads": 2,
        "model": "",
        "voice": "",
        "instruction": "",
        "key_env": "VOX_DASHSCOPE_KEY",
    },
    "input": {
        "device": "",
        "sample_rate": 16000,
        "blocksize": 1600,
        # 自适应输入增益。默认开 —— 实测这套东西的可用音量窗口很窄（Windows 输入音量
        # 默认 100 时削波、调到 7 才开始命中），而窗口位置取决于用哪只麦克风、戴不戴耳机、
        # 离多远。让人每次自己去声音设置里试是把工程问题外包给用户。
        # 它修的是「偏轻」那一端；削波救不回来（发生在 ADC 里），只报告。见 core/audio/gain.py。
        "auto_gain": True,
    },
    # visible 默认 false = 待机时桌面上没有球。它在唤醒命中之后才弹出，回合结束几秒后
    # 收回去 —— 一个常驻在桌面上的球是个永久的视觉噪声，而它 99% 的时间无事可做。
    "orb": {"enabled": True, "visible": False, "hide_after_s": 10.0},
}


class VoiceConfigError(RuntimeError):
    """A voice config that cannot be trusted to mean what it says."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def model_paths() -> dict[str, Path]:
    """The four model locations, environment first, ``models/`` second.

    Returned even when the files are absent: whether a model is *present* is a
    provider's ``available`` property to answer, and reporting "not found" with a
    concrete path beats reporting nothing at all.
    """
    models = repo_root() / "models"
    return {
        "kws_dir": Path(os.getenv("VOX_KWS_MODEL_DIR") or models / DEFAULT_KWS_DIR),
        "asr_dir": Path(os.getenv("VOX_ASR_MODEL_DIR") or models / DEFAULT_ASR_DIR),
        "tts_dir": Path(os.getenv("VOX_TTS_MODEL_DIR") or models / DEFAULT_TTS_DIR),
        "vad_model": Path(os.getenv("VOX_VAD_MODEL") or models / DEFAULT_VAD_MODEL),
    }


def default_voice_config() -> dict[str, Any]:
    """The shipped defaults, flat, with model paths resolved."""
    flat: dict[str, Any] = {}
    for section, keys in _SCHEMA.items():
        for key, value in keys.items():
            flat[f"{section}.{key}"] = value
    flat.update({name: str(path) for name, path in model_paths().items()})
    return flat


def _coerce(section: str, key: str, value: Any, default: Any) -> Any:
    """Type-check one value against its default, the way tools config does.

    ``bool`` is checked before ``int`` on purpose: in Python ``True`` is an
    ``int``, so a plain ``isinstance`` check would let ``enabled = 1`` through and
    ``num_threads = true`` as well.
    """
    where = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise VoiceConfigError(f"{where} must be a boolean")
        return value
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, int):
            raise VoiceConfigError(f"{where} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise VoiceConfigError(f"{where} must be a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise VoiceConfigError(f"{where} must be a string")
        return value
    raise VoiceConfigError(f"{where} has an unsupported type")


def load_voice_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/voice.toml``. A missing file yields the shipped defaults.

    Keys come back flattened as ``"section.key"`` so a caller reads
    ``config["tts.enabled"]`` without walking nested dicts, and the four resolved
    model paths ride along as plain strings under ``kws_dir`` / ``asr_dir`` /
    ``tts_dir`` / ``vad_model``.
    """
    config_path = Path(
        path or os.getenv("VOX_VOICE_CONFIG", repo_root() / "config" / DEFAULT_CONFIG_NAME)
    )
    merged = default_voice_config()
    if not config_path.is_file():
        return merged
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VoiceConfigError(f"voice config is unreadable: {exc}") from exc
    for section, values in raw.items():
        if section not in _SCHEMA:
            raise VoiceConfigError(f"unknown config section: [{section}]")
        if not isinstance(values, dict):
            raise VoiceConfigError(f"[{section}] must be a table")
        for key, value in values.items():
            if key not in _SCHEMA[section]:
                raise VoiceConfigError(f"unknown config key: {section}.{key}")
            merged[f"{section}.{key}"] = _coerce(section, key, value, _SCHEMA[section][key])
    return merged


def custom_keywords_path() -> Path:
    """自定义词表落在哪。``VOX_KEYWORDS_FILE`` 优先。

    走环境变量而不是只认仓库内的固定路径，和 ``models_config_path()`` 同一个模式：
    一个只会往仓库里某个硬编码位置写的入口没法测，而「控制台写词表」这条路正需要被测。
    """
    override = os.getenv("VOX_KEYWORDS_FILE", "").strip()
    if override:
        return Path(override)
    return repo_root() / DEFAULT_KEYWORDS_FILE


def resolve_keywords_file(config: dict[str, Any]) -> Path | None:
    """``wake.keywords_file`` as a path, or ``None`` for "use the model's own".

    A relative entry resolves against the repository root rather than the process
    working directory, so the same config works whether Vox was started from the
    repo, from a shortcut, or from a service manager.

    ``wake.keywords_file`` 留空时先看约定路径（``config/keywords.txt``，可由
    ``VOX_KEYWORDS_FILE`` 改）：文件在就用它。走约定而不是让界面去写那个键，是因为
    「读哪个文件」和「阈值是多少」不是一类设置 —— 前者是个文件系统入口，放进控制台的
    可编辑白名单等于让一个网页决定进程去读哪个路径。约定的代价是多一条规则要记，
    换来的是手改和界面改落在同一个文件上。
    """
    raw = str(config.get("wake.keywords_file", "")).strip()
    if not raw:
        default = custom_keywords_path()
        return default if default.is_file() else None
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else repo_root() / candidate


def resolve_device(config: dict[str, Any]) -> int | str | None:
    """``input.device`` as sounddevice wants it: index, name fragment, or None."""
    raw = str(config.get("input.device", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


__all__ = [
    "DEFAULT_ASR_DIR",
    "DEFAULT_KWS_DIR",
    "DEFAULT_TTS_DIR",
    "DEFAULT_VAD_MODEL",
    "DEFAULT_KEYWORDS_FILE",
    "custom_keywords_path",
    "VoiceConfigError",
    "default_voice_config",
    "load_voice_config",
    "model_paths",
    "repo_root",
    "resolve_device",
    "resolve_keywords_file",
]
