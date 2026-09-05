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
from core.audio.kws import DEFAULT_MAX_ACTIVE_PATHS

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
        # 解码束宽。**这是纯召回参数，不是判定标准** —— 唤醒词的假设路径要和普通转写路径
        # 竞争束里的位置，束太窄时它在噪声里会先被剪掉。sherpa-onnx 的默认是 4，实测那个
        # 值在 5 dB SNR 上开始丢命中、0 dB 只剩 2/5；16 在 0 dB 是 5/5，而每块耗时不变
        # （1.10 → 1.21 ms / 100 ms 块）。见 core/audio/kws.py 与 prototype-results.md。
        "max_active_paths": DEFAULT_MAX_ACTIVE_PATHS,
        # 唤醒确认音。命中之后先应一声再开始听 —— 没有这一声，人会以为没听见而重复喊，
        # 而重复的第二遍会落进已经开着的识别器。空字符串 = 关掉。分隔符收「，,；;|、」。
        "acks": DEFAULT_ACKS,
        # **连续对话**：回答说完之后不收话筒，直接等下一句（不用再喊唤醒词）。
        #
        # 窗口长度不在这里 —— 它是 config/speaker.toml 的 `listen_grace_s`（默认 8 秒）。
        # 两个数字各自可调的话它们一定会分岔，而分岔之后「球什么时候收」和「话筒什么时候
        # 关」就不是同一件事了，那正是「球还在但它已经不听了」的成因。
        #
        # 代价说清楚：这个窗口内**别人说话也会被当成已验证的那个人**（它是上一次声纹通过
        # 的延续，见 core/audio/capture.py 的 resume_listening），而且**回答正在播的时候
        # 喊唤醒词打断不了它** —— 播放期间挂着静音窗，否则助手会把自己的回答转写成下一句
        # 请求。要打断就等它说完，或者用托盘。设成 false 退回「每句都要先喊唤醒词」。
        "follow_up": True,
    },
    # 识别走本机还是云端。**2026-09-03 加的六个键** —— 在那之前 ASR 只有本机一条路。
    #
    # provider = "sherpa"     本机流式 zipformer，num_threads 生效
    # provider = "dashscope"  百炼 qwen-audio-3.0-asr-flash，model / key_env / 三个时长生效
    #
    # 为什么默认换成云端：同一条真录音，本机把「小沃」听成「小吴」而云端全对还带标点。
    # 差别不在阈值上 —— 本机那个 14M 模型的字表只有 1426 个汉字，「沃」不在里面，它
    # **写不出**这个字。见 core/audio/asr_cloud.py 模块头那张表。
    #
    # 代价说清楚：**被接受的唤醒之后说的那句话会以 base64 出网。** 唤醒词与声纹仍然
    # 一步都不出网（那两件事在 KWS 与声纹门里，都是本机）。不写盘、不留缓存。
    "asr": {
        "enabled": True,
        "provider": "sherpa",
        "num_threads": 2,
        "model": "",
        "key_env": "VOX_ASR_KEY",
        # 尾部静音判「说完了」。云端往返本身要几秒，端点上省下的每 100 ms 都落在等待里。
        "silence_s": 0.8,
        # 一段最长多久。到了不等静音也发 —— 念清单、读地址那种没有句末的长句不该卡死整轮。
        "max_utterance_s": 30.0,
        "timeout_s": 60.0,
    },
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
        # 云端那条路走哪条线：``"ws"`` WebSocket（默认）· ``"sse"`` HTTP 分块 ·
        # ``"http"`` 两个往返。2026-09-05 实测同模型同音色整段 936 ms vs 3578 ms ——
        # 差的是 HTTP 层的固定开销，不是合成速度。只对 provider = "dashscope" 有意义。
        "wire": "ws",
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
    #
    # **renderer / size / show_text 是 2026-09-03 加的，它们此前只能靠环境变量传。**
    # 控制台的「唤醒球」那一栏当时只能**生成一行 `VOX_ORB_SIZE=140 VOX_ORB_RENDERER=bot`
    # 让人自己复制到启动环境里** —— 而使用者的判断是：一项配置「只能靠环境变量传」在他的
    # 使用路径里等于不存在。这三个键让那一栏变成真的能存、重启生效。
    #
    # 三个值最终仍然以环境变量交给球（`core/desktop_bridge.py` 注入，Rust 侧读它们拼进
    # URL query）—— 那条通道没变，变的是**谁来填**：配置文件填，不是人填。
    "orb": {
        "enabled": True,
        "visible": False,
        "hide_after_s": 10.0,
        # "seq" = AE 预渲染序列（第十一代，出厂默认）；"bot" = bloub 有脸的实体球（第十二代）。
        # 只认这两个值，拼错的落回 "seq" 而不是落在一个空白的球上（Rust 侧同款立场）。
        "renderer": "seq",
        # 布局盒边长，96–420。越界会被钳制并报警告，而不是静默取默认。
        "size": 140,
        # 平时出不出文字。报错与拒绝无论这个开关都出文字。
        "show_text": False,
    },
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


#: 唤醒球的渲染层。只认这两个值。
#:
#: 拼错落回 ``seq`` 而不是落在一个空白的球上 —— Rust 侧的 `VOX_ORB_RENDERER` 判的是
#: `v.trim() == "bot"`，同款立场（**只认一个值**）。两处必须同时改。
ORB_RENDERERS: tuple[str, ...] = ("seq", "bot")

#: 布局盒边长的钳制范围，与 `desktop/src-tauri/src/main.rs` 里那个 `(96..=420)` 一致。
#: 那一侧越界直接忽略；这一侧越界钳制**并报警告** —— 配置文件里写了个数字而它悄悄
#: 变成别的，是「看起来配了但其实没生效」的一种。
ORB_SIZE_MIN = 96
ORB_SIZE_MAX = 420


def orb_environment(config: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """`[orb]` 的三项 -> 交给球那个子进程的环境变量，以及要报的警告。

    球是另一个进程，它读的是自己的 env（Rust 侧 `setup` 里把它们拼进 URL query）。所以
    这一层的职责只有一个：**把配置文件翻译成那三个变量**。通道没变，变的是谁来填 ——
    在这个函数之前只有人能填（控制台生成一行让人复制），而那等于这项配置不存在。

    返回的 dict 只含**需要设**的那些：默认值不写进 env，好让一个手动设了
    ``VOX_ORB_RENDERER`` 的 shell 仍然赢（调试时那条路必须留着）。
    """
    warnings: list[str] = []
    env: dict[str, str] = {}

    renderer = str(config.get("orb.renderer", "seq") or "").strip().lower()
    if renderer not in ORB_RENDERERS:
        warnings.append(
            f"orb.renderer = {renderer!r} 不认识（只有 {' / '.join(ORB_RENDERERS)}），按 seq 处理"
        )
        renderer = "seq"
    if renderer == "bot":
        env["VOX_ORB_RENDERER"] = "bot"

    raw_size = config.get("orb.size", 140)
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        warnings.append(f"orb.size = {raw_size!r} 不是整数，按 140 处理")
        size = 140
    if not ORB_SIZE_MIN <= size <= ORB_SIZE_MAX:
        clamped = max(ORB_SIZE_MIN, min(ORB_SIZE_MAX, size))
        warnings.append(f"orb.size = {size} 越界，钳制到 {clamped}（{ORB_SIZE_MIN}–{ORB_SIZE_MAX}）")
        size = clamped
    if size != 140:
        env["VOX_ORB_SIZE"] = str(size)

    if bool(config.get("orb.show_text", False)):
        env["VOX_SHOW_TEXT"] = "1"
    return env, warnings


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


#: 除了类型，还要检查**取值**的那几个键。返回错误信息，``None`` = 通过。
#:
#: 类型对而值离谱的键是「看起来配了但其实没生效」的另一种形状：`orb.size = 900` 是个
#: 合法整数，可球只能在 96–420 之间，于是它在启动时被悄悄钳成 420 —— 而使用者在控制台上
#: 看到的是自己填的 900。所以这里**报错**，和「拼错的键报错而不是被忽略」同一条立场。
#:
#: 这也是控制台那一侧的校验器：`/api/config` 写之前会用这个加载器试一遍
#: （`core/console/routes.py::_validator`），所以页面上填 900 会被**当场拒绝并说明范围**，
#: 而不是存下去再在下一次启动时变成别的数。
_VALUE_CHECKS: dict[str, Any] = {
    "orb.renderer": lambda value: (
        None
        if str(value).strip().lower() in ORB_RENDERERS
        else f"只能是 {' / '.join(ORB_RENDERERS)}"
    ),
    "orb.size": lambda value: (
        None
        if ORB_SIZE_MIN <= int(value) <= ORB_SIZE_MAX
        else f"要在 {ORB_SIZE_MIN}–{ORB_SIZE_MAX} 之间（球的布局盒）"
    ),
    "orb.hide_after_s": lambda value: (
        None if float(value) <= 3600 else "最多 3600 秒 —— 再长就该用 orb.visible = true"
    ),
    # 阈值不是「越大越安全」：0.95 会把本人也拒掉，而 0.05 等于没有门。真正的值靠
    # REAL-MIC 实测定（发布阻塞项 #1），这里只挡住明显不可能的输入。
    "wake.keywords_threshold": lambda value: (
        None if 0.05 <= float(value) <= 0.95 else "要在 0.05–0.95 之间"
    ),
    # 解码束宽。实测 4 在 0 dB 只剩 2/5、16 是 5/5，而每块耗时几乎不变；再往上收益递减
    # 而误唤醒的证据不够。64 是这一层愿意接受的上限，不是推荐值。
    "wake.max_active_paths": lambda value: (
        None if 1 <= int(value) <= 64 else "要在 1–64 之间（推荐 16，见 core/audio/kws.py）"
    ),
    # 云端映射成 rate，超出这个区间百炼直接拒；本机是 length_scale 的倒数，太极端会失真。
    "tts.speed": lambda value: (None if 0.5 <= float(value) <= 2.0 else "要在 0.5–2.0 之间"),
    # 块长决定唤醒的响应粒度。太小 CPU 上升、太大唤醒变钝；16000 = 1 秒已经明显迟钝。
    "input.blocksize": lambda value: (
        None if 160 <= int(value) <= 16000 else "要在 160–16000 之间（1600 = 100 ms/块）"
    ),
    # 云端识别的端点参数。下限不是审美：0.2 s 的尾部静音会把句中换气切成两句（换气实测
    # 0.2–0.5 s），而那时上半句先被发上去、下半句变成第二次请求，表现是「它总打断我」。
    "asr.silence_s": lambda value: (
        None if 0.3 <= float(value) <= 5.0 else "要在 0.3–5.0 之间（0.8 是实测起点）"
    ),
    # 一段的长度上限。30 s 的 16 kHz 单声道 base64 后约 1.3 MB；120 s 是这一层愿意
    # 塞进一个 JSON 请求体的上限，不是推荐值。
    "asr.max_utterance_s": lambda value: (
        None if 2.0 <= float(value) <= 120.0 else "要在 2–120 秒之间"
    ),
    "asr.timeout_s": lambda value: (
        None if 5.0 <= float(value) <= 300.0 else "要在 5–300 秒之间"
    ),
}


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
            flat = f"{section}.{key}"
            coerced = _coerce(section, key, value, _SCHEMA[section][key])
            check = _VALUE_CHECKS.get(flat)
            if check is not None:
                problem = check(coerced)
                if problem:
                    raise VoiceConfigError(f"{flat} = {coerced!r} 不行：{problem}")
            merged[flat] = coerced
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


#: 按名字选设备时，host API 的优先顺序。
#:
#: Windows 上**同一个物理麦克风会以四种 host API 各出现一次**（MME / DirectSound /
#: WASAPI / WDM-KS），所以把名字片段直接交给 sounddevice 会换回
#: `Multiple input devices found` 而不是一个设备 —— 那正是 `config/voice.toml` 里那句
#: 「填索引，不要填名字片段」的由来。可是索引**也不稳**：它由枚举顺序决定，插拔一个设备
#: 就会移位。2026-08-29 记下的 `device = "2"` 当时是耳机，2026-09-01 同一个索引指向的是
#: 麦克风阵列 —— 配置文件里的注释还写着「[2] 耳机」，而症状是唤醒率变差，不是报错。
#:
#: 所以名字要能用，歧义要在这里消掉。WASAPI 排第一是因为它是 Windows 上的现代采集路径，
#: 而且这台机器上耳机那一条只有 WASAPI 报了 16 kHz（其余报 44.1 kHz，要重采样）。
_HOST_API_PREFERENCE = ("wasapi", "mme", "directsound", "wdm-ks")

#: 按名字选设备时**直接排除**的 host API。
#:
#: WDM-KS 是 PortAudio 暴露出来的内核流路径。它在清单里看着是个候选，
#: ``check_input_settings`` 也说这个格式可以 —— 然后 ``start()`` 抛
#: ``Unanticipated host error``。2026-09-01 在这台机器上两个方向各抓到一次：
#: 输出 ``GLE = 0x00000490``、输入 ``GLE = 0x0000048F``（都是蓝牙 hands-free 那两条）。
#:
#: 所以它不能当「最后的退路」：选中一个开不起来的设备比一个都没选中更糟 ——
#: 前者的报错是一串 IOCTL 错误码，后者是「你配的那只麦克风现在不在」。名字解析不到时
#: ``open_voice_stack`` 会给后面那句话。真要用 WDM-KS 的人可以在配置里写索引，
#: 那条路没有被关掉。
_EXCLUDED_HOST_APIS = ("wdm-ks",)


def usable_input(index: int, api_name: str, sample_rate: int = 16000) -> str:
    """这条设备条目能不能当 ``input.device`` 用。返回空串 = 能用，否则是不能用的原因。

    **和 ``_match_device`` 用同一套判据**，这是它存在的全部理由：控制台的设备选择器如果
    列出一条 `_match_device` 会拒掉的条目，人点了它就等于回到「配了一个不生效的设备名」——
    而那正是「用笔记本内置麦克风时读不到设备」那条报告的形状（栈静默退回系统默认）。
    一个选择器只该给出解析器认得的选项。
    """
    api = str(api_name).casefold().replace(" ", "")
    for excluded in _EXCLUDED_HOST_APIS:
        if excluded in api:
            return f"{api_name} 这条路开不起来（PortAudio 内核流，实测 start() 抛 host error）"
    try:
        import sounddevice  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 装不上就别替它下结论
        return ""
    try:
        sounddevice.check_input_settings(
            device=int(index), channels=1, samplerate=int(sample_rate), dtype="float32"
        )
    except Exception as exc:  # noqa: BLE001 - 打不开就不是候选
        return f"按 {sample_rate} Hz 单声道打不开：{exc}"
    return ""


def _match_device(fragment: str, sample_rate: int = 16000) -> int | str:
    """名字片段 -> 设备索引。

    ``sounddevice`` 装不上或枚举失败时原样返回：这个模块是配置层，不该因为一个可选依赖
    缺失而让读配置失败。

    三步收窄，每一步都有实测理由：

    1. **只留能真的按这个采样率打开的**（``check_input_settings``）。这一步删掉的不是理论
       上的候选：WDM-KS 那几条蓝牙 hands-free 条目常常只报 8 kHz，而 Realtek 阵列在
       WASAPI 下只接受 2 通道 —— 它们在清单里看着像候选，一打开就抛。
    2. **按 host API 优先级排**（见 ``_HOST_API_PREFERENCE``）。
    3. **还并列就取最小索引，不再原样返回。**

    第 3 步是 2026-09-01 改的。此前并列时返回名字片段，让 sounddevice 去抛
    ``Multiple input devices found`` —— 理由是「真歧义不猜」。实测那个理由站不住：这台机器
    上蓝牙耳机与一只蓝牙音箱的名字**都含「耳机」**，而蓝牙设备在两次枚举之间会出现/消失，
    于是同一份配置有时解析成 WASAPI 的那一条、有时只剩两条 WDM-KS 并列 —— 后者直接让
    麦克风开不起来。**设备选择不是安全边界**（安全边界是声纹门），在这里 fail-closed 保护
    不了任何东西，只是把「可能选错一只麦克风」换成了「一只都没有」。选中的是哪一个由
    ``describe_device()`` 报出来，就绪清单与启动日志都印它，所以选错是看得见的。
    """
    try:
        import sounddevice  # noqa: PLC0415 - 可选依赖，只在真要选设备时才导
    except Exception:  # noqa: BLE001
        return fragment
    try:
        devices = sounddevice.query_devices()
        apis = sounddevice.query_hostapis()
    except Exception:  # noqa: BLE001 - 设备枚举失败时退回原值
        return fragment

    def openable(index: int) -> bool:
        try:
            sounddevice.check_input_settings(
                device=index, channels=1, samplerate=int(sample_rate), dtype="float32"
            )
        except Exception:  # noqa: BLE001 - 打不开就不是候选
            return False
        return True

    needle = fragment.casefold()
    candidates: list[tuple[int, int]] = []
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        if needle not in str(device.get("name", "")).casefold():
            continue
        api = str(apis[int(device["hostapi"])].get("name", "")).casefold().replace(" ", "")
        if any(excluded in api for excluded in _EXCLUDED_HOST_APIS):
            continue
        rank = next(
            (position for position, key in enumerate(_HOST_API_PREFERENCE) if key in api),
            len(_HOST_API_PREFERENCE),
        )
        candidates.append((rank, index))
    if not candidates:
        return fragment
    usable = [entry for entry in candidates if openable(entry[1])]
    # 一条都打不开时不要把候选清空 —— 那会退回名字片段并抛一个更难读的错。
    pool = usable or candidates
    best = min(rank for rank, _ in pool)
    return min(index for rank, index in pool if rank == best)


def resolve_device(config: dict[str, Any]) -> int | str | None:
    """``input.device`` as sounddevice wants it: index, name fragment, or None.

    数字原样当索引用。**名字片段现在会被解析成索引**（见 ``_match_device``）—— 索引会随
    插拔移位，而名字不会，所以按名字配才是能跨重启活下来的那一种。
    """
    raw = str(config.get("input.device", "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return _match_device(raw, int(config.get("input.sample_rate", 16000) or 16000))


def describe_device(device: int | str | None) -> str:
    """解析后的设备是哪一个，给就绪清单和启动日志用。

    存在的理由是 2026-09-01 那次索引漂移：配置里写着 `2`，注释里写着「耳机」，而实际打开
    的是麦克风阵列。**报出名字**是让这类漂移在下一次发生时立刻可见的唯一办法。
    """
    if device is None:
        label = "系统默认"
    else:
        label = str(device)
    try:
        import sounddevice  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return label
    try:
        info = sounddevice.query_devices(device if device is not None else None, "input")
        api = sounddevice.query_hostapis(int(info["hostapi"]))["name"]
    except Exception as exc:  # noqa: BLE001 - 报不出来也不能抛
        return f"{label}（查不到：{type(exc).__name__}）"
    return f"{label} = {info['name']}（{api}）"


__all__ = [
    "DEFAULT_ASR_DIR",
    "DEFAULT_KWS_DIR",
    "DEFAULT_TTS_DIR",
    "DEFAULT_VAD_MODEL",
    "DEFAULT_KEYWORDS_FILE",
    "ORB_RENDERERS",
    "ORB_SIZE_MAX",
    "ORB_SIZE_MIN",
    "custom_keywords_path",
    "VoiceConfigError",
    "default_voice_config",
    "describe_device",
    "load_voice_config",
    "model_paths",
    "orb_environment",
    "repo_root",
    "resolve_device",
    "resolve_keywords_file",
]
