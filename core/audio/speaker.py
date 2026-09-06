"""Local speaker verification (声纹校验).

Only a previously enrolled voice may drive the platform. This runs entirely on
the existing sherpa-onnx runtime -- no new dependency, no network call.

Two red-line consequences are enforced here:

* Audio is never persisted. ``enroll`` and ``verify`` accept in-memory sample
  chunks and keep only the resulting embedding vectors.
* Enrollment data is biometric. It lives outside the repository tree by default
  and is listed in ``.gitignore``; ``describe`` never returns raw vectors.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .base import ProviderStatus, ProviderUnavailable

#: 出厂的说话人 embedding 模型。
#:
#: **2026-08-29 从 `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx` 换成 CAM++。**
#: 判据不是「官方说它好」，是在本仓库自带的 7 段真实人声上量出来的（同一次进程、同一段音频、
#: 同一个余弦度量；分组来自实测矩阵而不是任何标签）：
#:
#: | 模型 | 大小 | dim | 同人最低 | 不同人最高 | **间隙** |
#: |---|---|---|---|---|---|
#: | ERes2Net base | 37.8 MB | 512 | 0.736 | 0.370 | +0.366 |
#: | **CAM++ zh-cn common** | **27.0 MB** | **192** | **0.833** | 0.396 | **+0.437** |
#:
#: 同人分整体抬高约 0.1 而不同人分几乎没动，所以间隙宽了 0.07；模型还小了 10.8 MB、
#: 向量短了 2.7 倍（打分更快）。这对使用者报的「声纹识别率有点低」是直接有效的一步 ——
#: 他的实测分数落在 0.34–0.48，同人分整体上移就是要的那个方向。
#:
#: **换型让既有档案全部作废**（512 维的向量对 192 维的模型无意义）。`_restore()` 会把它们
#: 丢掉并记进 `stale_profiles`，所以那件事是可见的，不是「一个人都没注册」。换完必须重新注册：
#: `.\.venv\Scripts\python.exe scripts\enroll_speaker.py 你的名字 --replace`
DEFAULT_MODEL_NAME = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
DEFAULT_CONFIG_NAME = "speaker.toml"
STORE_VERSION = 1

#: 文件名 -> 人看的短名。
#:
#: 存在的理由是 2026-08-30 使用者的原话：「我无法快速的识别是否真的换了新的声纹模型」。
#: `describe()` 报的 `model` 是一条绝对路径，而「换过型没有」这个问题要在**一眼之内**
#: 答完。放在这里而不是页面里：关于模型的知识只该有一个出处，页面再抄一份就会在下一次
#: 换型时静默过期。
MODEL_LABELS: dict[str, str] = {
    "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx": "CAM++ zh-cn common",
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx": "ERes2Net base（已取代）",
}


def model_label(path: str | Path) -> str:
    """一条模型路径 -> 短名。表里没有就退回文件名（**不退回整条路径**）。"""
    name = Path(path).name
    return MODEL_LABELS.get(name, name)

#: 进 embedding 模型前归一化到的峰值。
#:
#: 0.7 而不是 1.0：留 3 dB 余量。归一化到满幅会让任何后续处理（重采样的插值、模型内部的
#: 预加重）碰到轨，而那等于自己制造削波。
EMBED_TARGET_PEAK = 0.7

#: 一段音频里至少要有这么多秒**语音**，说话人嵌入才稳。低于它只写进拒绝原因，
#: **不参与判决** —— 一个「语音太少所以拒绝」的判据会把「短促但真的是你」也挡掉，
#: 而那正是使用者报的症状本身。
#:
#: 1.5 秒来自 2026-09-02 的真机验收（REAL-MIC）：相似度 0.506 / 0.548 / 0.556 / 0.568，
#: 阈值 0.5，两次被拒 0.448 —— 余量只有百分之几。使用者的观察给出了机制：「只说唤醒词
#: 过不了，『你好小沃，现在几点了』能过」。校验窗 1.5 秒里「你好小沃」只占约 0.8 秒，
#: 另一半是静音；说话人嵌入在语音不足一两秒时明显退化，这是这类模型公认的性质。
#:
#: 所以这个常数的用途是**把不可见的原因变成一句能照着做的话**，不是加一道新的门。
MIN_VOICED_FOR_STABLE_EMBEDDING = 1.5

#: 低于这个峰值就不归一化。一段几乎无声的缓冲被放大 100 倍之后是一段响亮的噪声，
#: 而它算出来的 embedding 毫无意义 —— 那种输入该被质量门拒掉，不该被放大。
EMBED_MIN_PEAK = 1e-3


def normalise_for_embedding(samples: Any, target_peak: float = EMBED_TARGET_PEAK) -> Any:
    """按峰值归一化一段音频，给声纹模型用。

    **注册与校验必须走同一个函数**，否则两侧电平不同，相似度就测的是「音量差」而不是
    「说话人差」。见 ``SpeakerVerificationProvider.embed`` 的注释里那组实测数字。
    """
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    if values.size == 0:
        return values
    peak = float(np.max(np.abs(values)))
    if peak < EMBED_MIN_PEAK:
        return values
    return (values * (float(target_peak) / peak)).astype(np.float32)


def load_speaker_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read ``config/speaker.toml``, falling back to the built-in defaults.

    ``tomllib`` is standard library from 3.11, so configurability costs no new
    dependency. A missing file is not an error -- the shipped defaults are the
    secure ones (``require_verification = true``), and the fallback must not be
    the moment protection quietly turns off.
    """
    root = Path(__file__).resolve().parents[2]
    config_path = Path(path or os.getenv("VOX_SPEAKER_CONFIG", root / "config" / DEFAULT_CONFIG_NAME))
    defaults: dict[str, Any] = {
        "require_verification": True,
        "threshold": 0.5,
        "min_verify_seconds": 0.6,
        "min_enroll_seconds": 1.5,
        "buffer_seconds": 3.0,
        "verify_seconds": 1.5,
        #: 唤醒之后给多少秒开口。见 `capture._recognize` —— 端点检测在**没听到任何语音**
        #: 时 2.4 秒就触发一次（`rule1_min_trailing_silence`），此前那一下会直接把聆听结束
        #: 掉、还不通知任何人：球一直停在「在听」，而采集已经回到唤醒模式了。
        "listen_grace_s": 8.0,
        # Gate-hardening limits (2026-08-24). Quality floors reject junk audio
        # before it reaches the model; the cooldown throttles brute-force wake
        # attempts. They are heuristics, NOT anti-replay spoof detection --
        # ADR 002's limitation stands until a dedicated spoof model lands.
        "min_rms": 0.002,
        "max_clip_ratio": 0.05,
        "verify_windows": 1,
        "max_consecutive_rejections": 5,
        "cooldown_s": 30.0,
    }
    if not config_path.is_file():
        return defaults
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProviderUnavailable(f"speaker config is unreadable: {exc}") from exc
    merged = dict(defaults)
    for section in ("speaker", "capture"):
        for key, value in (raw.get(section) or {}).items():
            if key in merged:
                merged[key] = value
    return merged


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one gate decision.

    ``accepted`` is the only field the wake path should branch on. ``score`` is
    the cosine similarity against the best matching enrolled speaker and is
    reported for diagnostics and threshold tuning.
    """

    accepted: bool
    speaker: str | None
    score: float
    reason: str


@dataclass(frozen=True)
class EnrollmentResult:
    speaker: str
    samples_used: int
    total_seconds: float
    dim: int


class SpeakerStore:
    """Embedding-only persistence for enrolled speakers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, list[list[float]]]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderUnavailable(f"speaker enrollment store is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            raise ProviderUnavailable("speaker enrollment store has an unsupported version")
        speakers = raw.get("speakers")
        if not isinstance(speakers, dict):
            return {}
        return {
            name: [[float(x) for x in vector] for vector in vectors]
            for name, vectors in speakers.items()
            if isinstance(vectors, list) and vectors
        }

    def save(self, speakers: dict[str, list[list[float]]], *, dim: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": STORE_VERSION, "dim": dim, "speakers": speakers}
        # Write through a temp file so an interrupted save cannot corrupt an
        # existing enrollment.
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)


class SpeakerVerificationProvider:
    """Lazy, local speaker verification through sherpa-onnx.

    Loading is deferred exactly like the other providers: constructing this
    object never reads the model, and a missing model yields an unavailable
    ``ProviderStatus`` instead of an import-time crash.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        store_path: str | Path | None = None,
        threshold: float = 0.5,
        min_verify_seconds: float = 0.6,
        min_enroll_seconds: float = 1.5,
        num_threads: int = 1,
        provider: str = "cpu",
        min_rms: float = 0.002,
        max_clip_ratio: float = 0.05,
        verify_windows: int = 1,
        max_consecutive_rejections: int = 5,
        cooldown_s: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.model_path = Path(
            model_path or os.getenv("VOX_SPEAKER_MODEL", root / "models" / DEFAULT_MODEL_NAME)
        )
        self.store = SpeakerStore(
            store_path or os.getenv("VOX_SPEAKER_ENROLLMENT", root / "enrollment" / "voiceprints.json")
        )
        self.threshold = threshold
        self.min_verify_seconds = min_verify_seconds
        self.min_enroll_seconds = min_enroll_seconds
        self.num_threads = num_threads
        self.execution_provider = provider
        self.min_rms = min_rms
        self.max_clip_ratio = max_clip_ratio
        #: >1 splits the buffer into equal windows and requires every one of
        #: them to match the same speaker. Default 1 keeps the single-window
        #: decision; the stricter setting needs REAL-MIC tuning before use.
        self.verify_windows = max(1, int(verify_windows))
        self.max_consecutive_rejections = max(0, int(max_consecutive_rejections))
        self.cooldown_s = cooldown_s
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        # Brute-force throttle state. Counts only -- no audio, no vectors.
        self._rejection_streak = 0
        self._last_rejection_at = 0.0
        self._cooldown_until = 0.0
        self.gate_stats = {
            "accepted": 0,
            "rejected_below_threshold": 0,
            "rejected_quality": 0,
            "rejected_cooldown": 0,
            "consecutive_rejections": 0,
        }
        self._extractor: Any = None
        self._manager: Any = None
        self._dim = 0
        #: 因维度不符而被丢弃的档案 -> 丢了几条向量。换 embedding 模型时非空。
        #: 报出来是必需的：症状（一个人都没注册）和「文件丢了」长得一样。
        self.stale_profiles: dict[str, int] = {}

        #: --- 存储变更检测 -------------------------------------------------
        #:
        #: **这是「注册完了却唤不醒」的修法。** 档案落在文件里，但校验比的是内存里的
        #: `SpeakerEmbeddingManager`，而它只在 `load()` 时装载一次。于是：
        #:
        #: - `scripts/enroll_speaker.py` 在**另一个进程**里注册 -> 文件变了，正在跑的
        #:   控制台一无所知，门继续拿旧档案（或者一个空的档案表）打分；
        #: - 更糟的组合（2026-08-30 实机）：控制台 02:51 启动、页面上删掉一个人、脚本
        #:   02:55 注册了新的 —— 删除在同进程内生效（manager 也删了），新增只落了文件，
        #:   于是门的档案表是**空的**，每次唤醒都拒，理由 `no speaker enrolled`。
        #:   而脚本自己的闭环校验是 0.819「通过」，控制台页面上也显示新档案在 —— 三处
        #:   读数各说各话，唯独真正做决定的那一处是旧的。
        #:
        #: 指纹用 (mtime_ns, size) 而不是内容哈希：这是每次唤醒都要做一次的 `stat()`，
        #: 而唤醒本来就不频繁。同尺寸同 mtime 的覆写理论上会被漏掉，NTFS 的 100 ns
        #: 分辨率让它不会在真实使用里发生。
        self._store_stamp_seen: tuple[int, int] | None = None
        #: 重装是「造一个新的 manager 装满，再一次赋值换过去」，锁只为避免两个线程
        #: （音频线程做校验、HTTP 线程读状态）同时重复造。
        self._refresh_lock = threading.Lock()
        #: 重装次数与失败次数。失败时**保留旧档案**（一个空的门比一个旧的门更糟），
        #: 所以这个数字是唯一能看出「重装一直在失败」的地方。
        self.store_reloads = 0
        self.store_reload_errors = 0

    @property
    def available(self) -> bool:
        return self.model_path.is_file()

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def speakers(self) -> list[str]:
        if self._manager is None:
            return sorted(self.store.load())
        return sorted(self._manager.all_speakers)

    def load(self) -> ProviderStatus:
        if not self.available:
            return ProviderStatus(
                False, str(self.model_path), {"reason": "speaker verification model not found"}
            )
        try:
            sherpa = importlib.import_module("sherpa_onnx")
            config = sherpa.SpeakerEmbeddingExtractorConfig(
                model=str(self.model_path),
                num_threads=self.num_threads,
                provider=self.execution_provider,
            )
            if not config.validate():
                raise ValueError("invalid speaker embedding extractor configuration")
            self._extractor = sherpa.SpeakerEmbeddingExtractor(config)
            self._dim = self._extractor.dim
            self._manager = sherpa.SpeakerEmbeddingManager(self._dim)
        except Exception as exc:
            self._extractor = None
            self._manager = None
            return ProviderStatus(False, str(self.model_path), {"reason": f"speaker model load failed: {exc}"})
        enrolled = self._restore()
        return ProviderStatus(
            True,
            str(self.model_path),
            {"engine": "sherpa-onnx", "dim": self._dim, "enrolled": enrolled, "threshold": self.threshold},
        )

    def _restore(self, manager: Any = None) -> list[str]:
        """Re-register persisted embeddings into a fresh manager.

        ``manager`` 让调用方把档案装进一个**还没接上**的 manager 里，装满之后再一次赋值
        换过去（见 ``refresh``）。默认装进当前那个，也就是 ``load()`` 的用法。

        维度不符的向量被丢掉 —— 那是 fail-closed 的正确方向，但**必须说出来**：换一个
        embedding 模型（例如 ERes2Net dim 512 → CAM++ dim 192）会让全部既有档案作废，
        而症状是「一个人都没注册」，和「文件丢了」长得完全一样。2026-08-29 换 CAM++ 时
        这条注释和下面这个计数就是为那次换型加的。
        """
        target = self._manager if manager is None else manager
        # **先取指纹再读文件。** 反过来的话，两者之间发生的那次写入会被记成「已经看过」，
        # 于是那次注册永远不会被装载 —— 正是这个类要修的那个 bug 的另一种形态。
        stamp = self._store_stamp()
        restored: list[str] = []
        self.stale_profiles = {}
        for name, vectors in self.store.load().items():
            usable = [v for v in vectors if len(v) == self._dim]
            stale = len(vectors) - len(usable)
            if stale:
                self.stale_profiles[name] = stale
            if usable and target.add(name, usable):
                restored.append(name)
        self._store_stamp_seen = stamp
        return sorted(restored)

    def _store_stamp(self) -> tuple[int, int] | None:
        """存储文件的「变了没有」指纹。文件不在时是 ``None``。"""
        try:
            info = self.store.path.stat()
        except OSError:
            return None
        return (int(info.st_mtime_ns), int(info.st_size))

    def refresh(self) -> bool:
        """存储在盘上变过就重新装载档案，返回是否真的重装了。

        **门必须按盘上的档案判，不是按进程启动那一刻的快照判。** 另一个进程注册
        （`scripts/enroll_speaker.py`）、另一个控制台实例删档案、或者手工换掉
        `enrollment/voiceprints.json`，都只改文件；不重读的话，正在跑的那个进程会一直
        拿旧数据打分，而它的页面（`describe()` 直接读文件）却显示新数据 —— 两处读数
        矛盾，而做决定的是看不见的那一处。

        没变就什么都不做（一次 ``stat()``）。装载失败**保留旧档案**：把门清空会让本人
        也进不来，而那比「用旧档案多拒一次」严重得多。
        """
        if self._extractor is None or self._manager is None:
            return False
        if self._store_stamp() == self._store_stamp_seen:
            return False
        with self._refresh_lock:
            # 拿到锁之后再看一次：另一个线程可能已经重装完了。
            if self._store_stamp() == self._store_stamp_seen:
                return False
            try:
                sherpa = importlib.import_module("sherpa_onnx")
                fresh = sherpa.SpeakerEmbeddingManager(self._dim)
                self._restore(fresh)
            except Exception:  # noqa: BLE001 - 计数并保留旧档案
                self.store_reload_errors += 1
                return False
            self._manager = fresh
            self.store_reloads += 1
            return True

    def _require(self) -> None:
        if self._extractor is None:
            status = self.load()
            if not status.available:
                raise ProviderUnavailable(status.details["reason"])

    # -- embedding -----------------------------------------------------------

    def embed(self, samples: Any, sample_rate: int = 16000) -> list[float]:
        """Compute one embedding from in-memory samples.

        ``samples`` is any float32 buffer the sherpa stream accepts. It is
        consumed and dropped -- nothing reaches the filesystem.

        **进模型之前先按峰值归一化。** 这不是美化，是让注册和校验可比的唯一办法：

        - 注册走的路和校验走的路**音量不一样**。注册可能来自浏览器 `getUserMedia`
          （带浏览器自己的自动增益）或 `scripts/enroll_speaker.py`（`sd.rec` 裸录），
          校验来自 `sounddevice` 的采集回调。三条路的电平各不相同。
        - 2026-08-29 实测：同一个人、同一台机器，相似度稳定落在 0.339–0.484，也就是这个
          模型「不同人」的区间（它自己测试集上同人 0.736 / 不同人 0.370）。而使用者把
          Windows 输入音量从默认降到 **7** 之后唤醒才开始命中 —— 一个要靠调系统音量才能
          用的门，本质上是在要求人手动做归一化。
        - 归一化放在 `embed()` 里而不是采集里，是因为**这里是两条路唯一的交汇点**。放在
          采集里只归一化校验那一侧，注册那一侧仍然是原样，错配照旧。

        全静音不归一化（不做除零）—— 那种输入该被质量门拒掉，而不是被放大成噪声。
        """
        self._require()
        duration = len(samples) / float(sample_rate)
        if duration < self.min_verify_seconds:
            raise ProviderUnavailable(
                f"audio too short for speaker verification: {duration:.2f}s "
                f"< {self.min_verify_seconds}s"
            )
        stream = self._extractor.create_stream()
        stream.accept_waveform(
            sample_rate=sample_rate, waveform=normalise_for_embedding(samples)
        )
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise ProviderUnavailable("speaker extractor did not accept enough audio")
        return [float(x) for x in self._extractor.compute(stream)]

    # -- enrollment ----------------------------------------------------------

    def enroll(
        self, name: str, chunks: Iterable[Any], *, sample_rate: int = 16000
    ) -> EnrollmentResult:
        """Register or extend one speaker from several sample chunks.

        Existing vectors for ``name`` are kept and the new ones appended, so a
        weak enrollment can be improved without a full re-record.
        """
        self._require()
        name = (name or "").strip()
        if not name:
            raise ValueError("speaker name must not be empty")

        # 注册也必须过质量门,和 verify() 同一道 —— 而且**先把所有段验一遍再做任何嵌入**。
        #
        # **为什么这是必需的而不是对称性洁癖。** 此前只有 verify() 查质量,enroll() 不查,
        # 于是一段近乎无声的注册音频会被**接受**,落成几个噪声向量。后果不是「注册失败」
        # 而是「注册成功、然后本人永远唤不醒」—— 门看起来配好了、describe() 照报样本数、
        # 每次唤醒被拒的原因写着 below threshold,根因在注册那一侧却看不出来。
        #
        # 这条路真实存在:控制台注册走浏览器 getUserMedia,取系统默认输入设备,而 Windows
        # 上一个被静音/被隐私设置拒绝的默认设备是**静默而不是报错**的（见
        # core/audio/capture.py 的死麦克风检测,本机实测默认设备录 1 秒 peak=0.00003）。
        # 采集侧和注册侧会同时中招,而此前只有采集侧有检测。
        #
        # 全部先验再嵌入,有两个好处:坏在第三段时不会先白算两次嵌入,而且报出来的段号
        # 与模型是否可用无关 —— 拒绝的理由是音频本身,不该被一次嵌入失败盖过去。
        samples = list(chunks)
        if not samples:
            raise ValueError("enrollment needs at least one sample chunk")
        for index, chunk in enumerate(samples):
            issue = self._audio_quality_issue(chunk)
            if issue is not None:
                raise ProviderUnavailable(f"enrollment sample {index + 1} rejected: {issue}")

        total_seconds = sum(len(chunk) / float(sample_rate) for chunk in samples)
        if total_seconds < self.min_enroll_seconds:
            raise ProviderUnavailable(
                f"enrollment audio too short: {total_seconds:.2f}s "
                f"< {self.min_enroll_seconds}s"
            )
        vectors: list[list[float]] = [self.embed(chunk, sample_rate) for chunk in samples]
        speakers = self.store.load()
        speakers.setdefault(name, []).extend(vectors)
        self.store.save(speakers, dim=self._dim)
        # Re-register from scratch: the manager has no append-to-existing call.
        self._manager.remove(name)
        self._manager.add(name, speakers[name])
        return EnrollmentResult(name, len(vectors), total_seconds, self._dim)

    # -- verification --------------------------------------------------------

    def verify(
        self, samples: Any, *, sample_rate: int = 16000, throttle: bool = True
    ) -> VerificationResult:
        """Decide whether in-memory audio belongs to an enrolled speaker.

        ``throttle=False`` 把这一次校验从**暴力防护**里摘出去：不看冷却、不累计连续拒绝、
        不动 ``gate_stats``。**只给本机控制台的「试一句」用。**

        为什么必需（2026-08-31 实机）：控制台那颗按钮走的是同一个 `verify()`，于是它每一次
        失败都算作一次「连续拒绝」。使用者连点几下试一句（而那时它因为另一个 bug 固定返回
        0 分），第 5 次就把**真实唤醒门**推进了 30 秒冷却 —— 日志里是
        `声纹拒绝「你好小沃」：verification cooling down for 25.4s`，而使用者看到的是
        「说了唤醒词但根本没检测到」。一个本机、已鉴权、由人点出来的诊断不是暴力尝试，
        它不该消耗那个额度。

        This never raises for an ordinary rejection. Every failure path --
        cooldown, bad audio quality, model missing, nobody enrolled, embedding
        error, below threshold -- returns ``accepted=False``,
        so a caller that only branches on ``accepted`` is fail-closed by
        construction.
        """
        # Cheap input-side gates run before anything expensive or
        # environment-dependent, so their verdicts stay reachable even on a
        # host with no model installed.
        if throttle and self._cooldown_active():
            self.gate_stats["rejected_cooldown"] += 1
            remaining = round(self._cooldown_until - self._clock(), 1)
            return VerificationResult(
                False, None, 0.0, f"verification cooling down for {remaining}s"
            )
        quality = self._audio_quality_issue(samples)
        if quality is not None:
            self._tally("rejected_quality", throttle=throttle)
            return self._after_input_rejection(
                VerificationResult(False, None, 0.0, quality), throttle=throttle
            )
        try:
            self._require()
        except ProviderUnavailable as exc:
            return VerificationResult(False, None, 0.0, str(exc))
        # 盘上的档案变了就先重读。**这一步不能省**：另一个进程注册的档案只在文件里，
        # 而这里比的是内存里那份。见 `refresh` 的注释与 2026-08-30 的实机时间线。
        self.refresh()
        if self._manager.num_speakers == 0:
            return VerificationResult(False, None, 0.0, "no speaker enrolled")
        if self.verify_windows > 1:
            return self._verify_multi_window(samples, sample_rate, throttle=throttle)
        try:
            vector = self.embed(samples, sample_rate)
        except Exception as exc:
            return VerificationResult(False, None, 0.0, f"embedding failed: {exc}")
        name, score = self._best_match(vector)
        if name is None:
            return VerificationResult(False, None, 0.0, "no comparable enrollment")
        if score >= self.threshold:
            if throttle:
                self._rejection_streak = 0
                self.gate_stats["consecutive_rejections"] = 0
                self.gate_stats["accepted"] += 1
            return VerificationResult(True, name, score, "match")
        self._tally("rejected_below_threshold", throttle=throttle)
        # 把测出来的输入质量附上。「below threshold 0.5」单独出现时把人引向「调阈值」或
        # 「换模型」,而实机日志里真正的毛病是 peak=1.000 的削波 —— 那件事此前完全不可见,
        # 因为削波不到 5% 时质量门是放行的。见 input_quality 的注释。
        quality = self.input_quality(samples, sample_rate)
        # 差多少要说出来。0.448 和 0.05 是完全不同的两件事：前者是「条件不够好」，
        # 后者是「不是这个人」。只写「below threshold」把两者混成一句话。
        detail = (
            f"相似度 {score:.3f}，差 {self.threshold - score:.3f}"
            f"（阈值 {self.threshold}）"
        )
        if quality["clip"] > 0.0:
            detail += f"；输入削波 {quality['clip']:.1%}（峰值 {quality['peak']:.3f}）—— 麦克风增益偏高会削掉说话人特征"
        elif quality["rms"] < self.min_rms * 3:
            detail += f"；输入偏轻（rms {quality['rms']:.4f}）"
        # 语音太少是 2026-09-02 真机验收指向的那一条：说话人嵌入在语音不足一两秒时明显退化，
        # 而「你好小沃」只有约 0.8 秒。这句话要给出**能照着做的动作**，不是一个数字。
        voiced = quality["seconds"] * quality["active"]
        if voiced < MIN_VOICED_FOR_STABLE_EMBEDDING:
            detail += (
                f"；这一窗只有约 {voiced:.1f} 秒语音"
                f"（{quality['seconds']:.1f} 秒里 {quality['active']:.0%} 有声）—— "
                f"这个模型在语音不足 {MIN_VOICED_FOR_STABLE_EMBEDDING:.1f} 秒时同人分数会明显下降，"
                "把唤醒词和请求连着说（「你好小沃，现在几点了」）通常就过了"
            )
        return self._after_input_rejection(
            VerificationResult(False, None, score, detail), throttle=throttle
        )

    def _tally(self, key: str, *, throttle: bool) -> None:
        """记一次门的统计。``throttle=False``（试一句）不进统计 —— 一次诊断混进唤醒漏斗
        会让「门拒了几次」这个数字说不清是谁在敲门。"""
        if throttle:
            self.gate_stats[key] += 1

    def _cooldown_active(self) -> bool:
        """冷却是否仍在生效。**服刑期满即销账。**

        以前这里只是比一下时间，而 ``_rejection_streak`` 要等到「距上次拒绝超过 60 秒」
        才归零 —— 于是 30 秒的冷却结束后计数还停在 5，**再拒一次就立刻又是 30 秒**。
        实测（2026-08-29 使用者的实机日志）：`cooling down for 0.5s` → 一次真实校验
        0.484 → 紧接着 `cooling down for 25.2s`。也就是本人被压到**每 30 秒只有一次
        真实尝试**，而每次都在阈值边缘，于是永远进不来。

        暴力防护的目的是**限速**，不是累加惩罚。等满了就该重新给满额度：现在是
        「每 30 秒 5 次」，仍然挡得住穷举，但不会把一个正在调声纹的本人锁死。
        """
        if self._clock() < self._cooldown_until:
            return True
        if self._cooldown_until:
            self._cooldown_until = 0.0
            self._rejection_streak = 0
            self.gate_stats["consecutive_rejections"] = 0
        return False

    def _after_input_rejection(
        self, result: VerificationResult, *, throttle: bool = True
    ) -> VerificationResult:
        """Feed one input-driven rejection into the brute-force throttle.

        Model-missing and nobody-enrolled rejections say nothing about the
        input, so they never reach here -- only junk or unmatched audio does.
        A streak older than one cooldown period starts over: yesterday's
        pressure must not lock the owner out today.

        ``throttle=False`` 原样返回：控制台「试一句」的失败不是一次暴力尝试，
        不该占用本人的额度。见 ``verify`` 的注释与 2026-08-31 的实机日志。
        """
        if not throttle:
            return result
        now = self._clock()
        if now - self._last_rejection_at > max(self.cooldown_s, 60.0):
            self._rejection_streak = 0
        self._rejection_streak += 1
        self._last_rejection_at = now
        self.gate_stats["consecutive_rejections"] = self._rejection_streak
        if (
            self.max_consecutive_rejections
            and self._rejection_streak >= self.max_consecutive_rejections
        ):
            self._cooldown_until = now + self.cooldown_s
        return result

    def input_quality(self, samples: Any, sample_rate: int = 16000) -> dict[str, float]:
        """这段音频的 RMS、削波比例、时长与**有多少是语音**。给拒绝原因用的，不做判决。

        为什么需要它：使用者 2026-08-29 的实机日志里，每一次唤醒的块峰值都是 **1.000**，
        而拒绝原因只写「below threshold 0.5」。那句话把人引向「阈值不对」或「模型不行」，
        而真实情况是**输入在削波** —— 麦克风增益太高，波形贴着轨走，说话人特征被削掉了
        一部分，于是相似度稳定落在 0.34–0.48 这个「不同人」的区间里。

        削波不到 ``max_clip_ratio``（5%）时质量门是**放行**的，所以它此前完全不可见。
        把测出来的数字附在拒绝原因里，这件事就不用猜了。

        ``active`` 是 2026-09-02 加的，为了回答另一个问题。那天的真机验收（REAL-MIC）
        相似度是 0.506 / 0.548 / 0.556 / 0.568（阈值 0.5），两次被拒是 0.448 —— 余量只有
        百分之几。而使用者的观察把机制指了出来：

            仅使用唤醒词「你好小沃」会没有反应，且声纹过不了，
            但是「你好小沃，现在几点了」能过声纹。

        校验窗是 1.5 秒，而「你好小沃」只有约 0.8 秒 —— 窗里另一半是静音。说话人嵌入在
        语音不足一两秒时会明显退化，所以「窗里有多少是语音」正是要看的那个数。

        **不是 VAD**：这里只按能量比出「有多少帧不是静音」，判「是不是人声」是
        `core/audio/vad.py` 的事（那条判据必须是模型，见它的模块头）。这一个数只进
        拒绝原因，不参与任何判决，所以一个便宜的能量比在这里够用而且不会骗人。
        """
        values = np.asarray(samples, dtype=np.float32)
        if values.size == 0:
            return {"rms": 0.0, "clip": 0.0, "peak": 0.0, "seconds": 0.0, "active": 0.0}
        rms = float(np.sqrt(np.mean(np.square(values))))
        peak = float(np.max(np.abs(values)))
        # 20 ms 一帧，帧 rms 超过整段峰值 10% 的算「有声」。阈值取相对值而不是绝对值：
        # 一段轻的语音和一段响的语音都该量出差不多的语音占比。
        frame = max(1, int(0.02 * sample_rate))
        usable = values[: len(values) // frame * frame]
        if usable.size and peak > 0.0:
            frames = usable.reshape(-1, frame)
            energies = np.sqrt(np.mean(np.square(frames), axis=1))
            active = float(np.mean(energies >= 0.1 * peak))
        else:
            active = 0.0
        return {
            "rms": rms,
            "clip": float(np.mean(np.abs(values) >= 0.99)),
            "peak": peak,
            "seconds": len(values) / float(sample_rate),
            "active": active,
        }

    def _audio_quality_issue(self, samples: Any) -> str | None:
        """Reject silence or clipping before any model runs.

        Cheap, deterministic, testable without the model. These checks throw
        away garbage inputs; they are heuristics and do NOT detect replayed
        speech (ADR 002's limitation stands until a spoof model lands).
        """
        values = np.asarray(samples, dtype=np.float32)
        if values.size == 0:
            return "empty audio buffer"
        rms = float(np.sqrt(np.mean(np.square(values))))
        if rms < self.min_rms:
            return f"audio too quiet to verify (rms {rms:.5f} < {self.min_rms})"
        clip_ratio = float(np.mean(np.abs(values) >= 0.99))
        if clip_ratio > self.max_clip_ratio:
            return f"audio is clipped/saturated ({clip_ratio:.2f} at rail; limit {self.max_clip_ratio})"
        return None

    def _verify_multi_window(
        self, samples: Any, sample_rate: int, *, throttle: bool = True
    ) -> VerificationResult:
        """Every equal window must match the same speaker above threshold.

        Stricter than a single pass: flukes and short splices must survive
        every window instead of one. Needs REAL-MIC tuning of threshold and
        window count before production use.
        """
        values = np.asarray(samples, dtype=np.float32)
        window_length = len(values) // self.verify_windows
        minimum = int(self.min_verify_seconds * sample_rate)
        if window_length < minimum:
            return self._after_input_rejection(
                VerificationResult(
                    False,
                    None,
                    0.0,
                    f"not enough audio for {self.verify_windows}-window verification:"
                    f" {len(values)} samples < {self.verify_windows} x {minimum}",
                ),
                throttle=throttle,
            )
        best_score = 0.0
        agreed_speaker: str | None = None
        for index in range(self.verify_windows):
            chunk = values[index * window_length : (index + 1) * window_length]
            vector = self.embed(chunk, sample_rate)
            name, score = self._best_match(vector)
            best_score = max(best_score, score)
            if name is None or score < self.threshold:
                return self._after_input_rejection(
                    VerificationResult(
                        False,
                        None,
                        best_score,
                        f"window {index} below threshold {self.threshold}",
                    ),
                    throttle=throttle,
                )
            if agreed_speaker is None:
                agreed_speaker = name
            elif name != agreed_speaker:
                return self._after_input_rejection(
                    VerificationResult(
                        False,
                        None,
                        best_score,
                        f"windows disagree on speaker: {agreed_speaker} vs {name}",
                    ),
                    throttle=throttle,
                )
        if throttle:
            self._rejection_streak = 0
            self.gate_stats["consecutive_rejections"] = 0
            self.gate_stats["accepted"] += 1
        return VerificationResult(True, agreed_speaker, best_score, "all windows match")

    def _best_match(self, vector: list[float]) -> tuple[str | None, float]:
        """Best cosine score across enrolled speakers.

        ``SpeakerEmbeddingManager.search`` would only answer yes/no. Scoring each
        speaker also yields the number, which threshold tuning and the
        ``wake.rejected`` diagnostics both need.
        """
        best_name: str | None = None
        best_score = 0.0
        for name in self._manager.all_speakers:
            try:
                score = float(self._manager.score(name, vector))
            except Exception:
                continue
            if best_name is None or score > best_score:
                best_name, best_score = name, score
        return best_name, best_score

    # -- maintenance ---------------------------------------------------------

    def remove(self, name: str) -> bool:
        """Delete one speaker's enrollment from both the store and the manager.

        Works without a loaded model so enrollment can be cleaned up on a host
        that no longer has the model file.
        """
        speakers = self.store.load()
        existed = speakers.pop(name, None) is not None
        if existed:
            dim = self._dim or next(
                (len(v) for vectors in speakers.values() for v in vectors), 0
            )
            self.store.save(speakers, dim=dim)
        if self._manager is not None:
            self._manager.remove(name)
        return existed

    def describe(self) -> dict[str, Any]:
        """Status for ``diagnose()``: names and counts only, never raw vectors.

        Enrollment data is biometric, so this is the single sanctioned way to
        report on it. Callers must not reach into ``store`` directly.

        开头先 ``refresh()``：这份状态是给人看「门现在认谁」的，而 ``speakers`` 读的是
        文件、门读的是内存。不先对齐的话，页面会显示一个门还没看见的名字 —— 那正是
        2026-08-30 那次「明明重新录过了却认不出」里最误导人的一环。
        """
        self.refresh()
        try:
            speakers = self.store.load()
        except ProviderUnavailable as exc:
            return {
                "available": self.available,
                "model": str(self.model_path),
                "model_label": model_label(self.model_path),
                "store": str(self.store.path),
                "loaded": self._extractor is not None,
                "speakers": [],
                "reason": str(exc),
            }
        return {
            "available": self.available,
            "model": str(self.model_path),
            # 短名，给界面用。一眼看不出「换型了没有」的话，这份状态就答不了最常问的
            # 那个问题 —— 见 MODEL_LABELS 的注释。
            "model_label": model_label(self.model_path),
            "store": str(self.store.path),
            "loaded": self._extractor is not None,
            "dim": self._dim,
            "threshold": self.threshold,
            "speakers": sorted(speakers),
            "samples_per_speaker": {name: len(v) for name, v in speakers.items()},
            # 因 embedding 维度不符而作废的档案。**非空时上面那个 speakers 是骗人的** ——
            # 文件里有这个名字，但它的向量对当前模型无意义，门会照常拒绝。换 embedding
            # 模型（2026-08-29 ERes2Net dim 512 → CAM++ dim 192）就会出现这种状态。
            "stale_profiles": dict(self.stale_profiles),
            "needs_reenrollment": sorted(self.stale_profiles),
            "gate": {
                "min_rms": self.min_rms,
                "max_clip_ratio": self.max_clip_ratio,
                "verify_windows": self.verify_windows,
                "max_consecutive_rejections": self.max_consecutive_rejections,
                "cooldown_s": self.cooldown_s,
            },
            "gate_stats": dict(self.gate_stats),
            # 门**当前内存里**认的那些名字。和上面的 `speakers`（读文件）分开报：两者
            # 不一致就说明有人在这个进程之外改过档案，而那正是「注册了却唤不醒」的形状。
            # 正常情况下它们相同 —— `refresh()` 在这个方法开头已经把它们对齐了。
            "live_speakers": sorted(self._manager.all_speakers) if self._manager is not None else [],
            "store_reloads": int(self.store_reloads),
            "store_reload_errors": int(self.store_reload_errors),
        }

    def close(self) -> None:
        self._extractor = None
        self._manager = None
        self._dim = 0

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        **overrides: Any,
    ) -> SpeakerVerificationProvider:
        """Build a provider from ``config/speaker.toml``.

        Only the threshold and duration limits come from the file. Paths stay on
        environment variables so a config file checked into a repository can
        never point at somebody's enrollment data.
        """
        config = load_speaker_config(config_path)
        return cls(
            threshold=overrides.pop("threshold", config["threshold"]),
            min_verify_seconds=overrides.pop("min_verify_seconds", config["min_verify_seconds"]),
            min_enroll_seconds=overrides.pop("min_enroll_seconds", config["min_enroll_seconds"]),
            min_rms=overrides.pop("min_rms", config["min_rms"]),
            max_clip_ratio=overrides.pop("max_clip_ratio", config["max_clip_ratio"]),
            verify_windows=overrides.pop("verify_windows", config["verify_windows"]),
            max_consecutive_rejections=overrides.pop(
                "max_consecutive_rejections", config["max_consecutive_rejections"]
            ),
            cooldown_s=overrides.pop("cooldown_s", config["cooldown_s"]),
            **overrides,
        )



