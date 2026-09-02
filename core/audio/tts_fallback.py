"""云端合成失败时接着说话 —— 用本机那一把嗓子，并且**说出来它换了**。

## 为什么这个立场变了

2026-08-29 定的是「云端失败**不**降级到本机」，理由是「一个要求 longyuan 的人拿到 VITS
的默认女声会以为配置生效了」。那个理由本身没错，可它换来的代价 2026-09-02 在真机上出现了：
使用者的 `VOX_TTS_KEY` 被另一份 key 覆盖，百炼回 HTTP 401，而这条路径的三层
（`_open_tts` 只在 `load()` 失败时报警告、`complete_turn` 的 `except Exception: pass`、
云端 provider 自己不重试）合起来的结果是**助手一句话都不出声，而且哪里都不说为什么**。

对一个语音助手来说「不出声」是最坏的那一种失败：它和「没听见」「崩了」「网断了」在使用者
那一侧完全同形。所以现在的立场是**降级 + 大声说**，而不是静音：

- 换嗓子这件事会进运行日志（error 级）、进 `problems`，控制台的就绪清单读得到；
- 换过之后**这一次运行剩下的时间都用本机**（见 `latched` 那段），不每轮重试；
- 重启会重新试云端 —— 所以修好 key 之后不需要额外操作。

原来那个顾虑（「以为配置生效了」）由「大声说」这一半来解决，而不是由静音来解决。
"""

from __future__ import annotations

from typing import Any, Callable


class FallbackTts:
    """主合成器 + 备用合成器，同一个形状（红线 2：调用方不知道有两个）。

    ``on_problem`` 是可选的一个参数回调，收到的是一句给人看的话。它由运行时接到日志上；
    不接也不会丢，``problems`` 里留着。
    """

    def __init__(
        self,
        primary: Any,
        backup: Any,
        *,
        primary_label: str = "云端合成",
        backup_label: str = "本机合成",
        on_problem: Callable[[str], None] | None = None,
    ) -> None:
        self.primary = primary
        self.backup = backup
        self.primary_label = primary_label
        self.backup_label = backup_label
        self.on_problem = on_problem
        #: 主的那条路已经放弃了。**本次运行内不再重试** —— 401 这类失败每轮重试一次
        #: 只是每轮多等一个往返，而临时性的网络抖动重启就能恢复。
        self.latched = False
        self.problems: list[str] = []
        self.failures = 0

    # ---------------------------------------------------------------- 形状

    @property
    def active(self) -> Any:
        return self.backup if self.latched else self.primary

    @property
    def available(self) -> bool:
        return bool(getattr(self.primary, "available", False)) or bool(
            getattr(self.backup, "available", False)
        )

    def load(self) -> Any:
        # 已经放弃过就不再碰主的那条路：``load()`` 会被调用两次（建栈时一次、
        # 启动脚本再确认一次），而对一个 401 的端点重试一遍只是多等一个往返。
        if self.latched:
            return self.backup.load()
        status = self.primary.load()
        if getattr(status, "available", False):
            return status
        reason = ""
        try:
            reason = str((getattr(status, "details", None) or {}).get("reason", ""))
        except Exception:  # noqa: BLE001 - 报不出原因也要继续降级
            reason = ""
        self._give_up(f"{self.primary_label}起不来（{reason or '未说明原因'}）")
        return self.backup.load()

    def synthesize(self, text: str, **kwargs: Any) -> Any:
        return self._attempt("synthesize", text, **kwargs)

    def speak(self, text: str, **kwargs: Any) -> Any:
        return self._attempt("speak", text, **kwargs)

    def speak_segments(self, segments: Any, **kwargs: Any) -> Any:
        return self._attempt("speak_segments", segments, **kwargs)

    def stop(self) -> None:
        # 两个都停：切换发生在一次播放中间时，停的必须是真正在响的那一个。
        for provider in (self.primary, self.backup):
            try:
                provider.stop()
            except Exception:  # noqa: BLE001 - 停不下来不该抛给取消路径
                pass

    def is_stopped(self) -> bool:
        checker = getattr(self.active, "is_stopped", None)
        return bool(checker()) if callable(checker) else False

    def close(self) -> None:
        for provider in (self.primary, self.backup):
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass

    def describe(self) -> dict[str, Any]:
        """给就绪清单看：现在是哪一把嗓子，以及为什么。"""
        return {
            "active": self.backup_label if self.latched else self.primary_label,
            "degraded": self.latched,
            "failures": self.failures,
            "problems": list(self.problems),
        }

    # ---------------------------------------------------------------- 内部

    def _attempt(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.latched:
            try:
                return getattr(self.primary, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 这就是要降级的那一刻
                self.failures += 1
                self._give_up(f"{self.primary_label}失败（{type(exc).__name__}: {exc}）")
        return getattr(self.backup, method)(*args, **kwargs)

    def _give_up(self, why: str) -> None:
        """切到备用，并且只说一次。"""
        if self.latched:
            return
        self.latched = True
        backup_ok = bool(getattr(self.backup, "available", False))
        tail = (
            f"改用{self.backup_label}，本次运行不再重试（重启会重新试）"
            if backup_ok
            else f"而{self.backup_label}也不可用 —— 回答不出声"
        )
        message = f"{why}；{tail}"
        self.problems.append(message)
        if self.on_problem is not None:
            try:
                self.on_problem(message)
            except Exception:  # noqa: BLE001 - 报告通道失败不能改变合成的结果
                pass


__all__ = ["FallbackTts"]
