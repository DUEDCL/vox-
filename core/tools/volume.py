"""``system.volume`` —— 「声音大一点」。

## 为什么这一个必须是工具，而不是让 agent 说一句「你自己调吧」

它是**朗读期间**最自然的一句话，而那正是使用者手不在键盘上的时刻。让一个语音助手回答
「请在任务栏上点音量图标」是这个产品最没有说服力的一种回答。

## 三个动作，一个工具

    {}                    读：现在多大声
    {"level": 0.5}        设成 50%
    {"delta": 0.1}        调高 10%（**语音里最常见的那一种**：「大一点」没有数字）
    {"mute": true}        静音 / 取消静音

`delta` 与 `level` 同时给时 `level` 赢 —— 一个明确的数字比一个相对量更接近使用者的意思。

## 为什么只碰默认播放设备

说「声音大一点」的人指的是他此刻正在听的那个。要求他先说清「哪个设备」是把一句自然的话
变成一次配置。采集侧的 `read_level` 反过来要求精确设备名（那里选错设备等于「改了但没生效」，
而这一侧选错只会让另一只喇叭响一下，代价不对称）。

## 边界

* **可逆、无数据损失**，所以不需要确认卡。它和 `app.open` 同一档：动作的后果当场可听见。
* **`mute` 与 `level` 是两件事**：一个静音的设备把音量调到 100 仍然不出声，而使用者说
  「静音」时不希望他原来的音量被忘掉。
* **回答报的是重读之后的值**，不是请求值 —— 有些驱动只支持有级的音量，把请求值原样念回去
  等于报一个没发生的事。
* 非 Windows 上这个工具**不注册**（`winlevel` 是 Core Audio 的 ctypes 绑定）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .contract import ToolRequest, ToolResult

#: 「大一点 / 小一点」默认动多少。0.1 是一格 10% —— 实测这是「听得出变化但不会吓一跳」的
#: 一档，而使用者可以连着说两次。
DEFAULT_STEP = 0.1


class SystemVolumeTool:
    """读或改默认播放设备的音量。回答是一句能直接念的中文。"""

    name = "system.volume"

    def __init__(self, config: Mapping[str, Any] | None = None, *, backend: Any = None) -> None:
        self.config = dict(config) if config is not None else {}
        #: 注入的后端，测试用。生产上是 `core.audio.winlevel`。
        if backend is None:
            from core.audio import winlevel  # noqa: PLC0415 - 晚绑定：非 Windows 上可导入

            backend = winlevel
        self.backend = backend

    def describe(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "arguments": {
                "level": "设成这个大小，0–1（或 0–100 的整数）",
                "delta": f"相对调整，正负（默认一格 {DEFAULT_STEP}）",
                "mute": "true 静音 / false 取消静音",
            },
        }

    def run(self, request: ToolRequest) -> ToolResult:
        arguments = request.arguments or {}
        try:
            if "mute" in arguments:
                endpoint = self.backend.set_output_muted(_truthy(arguments.get("mute")))
                return self._done(endpoint, "静音" if endpoint.muted else "取消静音")
            level = _fraction(arguments.get("level"))
            delta = _fraction(arguments.get("delta"), signed=True)
            if level is None and delta is None:
                return self._done(self.backend.output_level(), "读")
            if level is None:
                current = self.backend.output_level()
                level = max(0.0, min(1.0, current.level + (delta or 0.0)))
            endpoint = self.backend.set_output_level(level)
        except Exception as exc:  # noqa: BLE001 - COM 的意外一律降级成一句可读的失败
            return ToolResult(
                tool=self.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                audit={"decision": "failed"},
            )
        return self._done(endpoint, "设")

    def _done(self, endpoint: Any, action: str) -> ToolResult:
        percent = int(round(float(getattr(endpoint, "level", 0.0)) * 100))
        muted = bool(getattr(endpoint, "muted", False))
        spoken = f"音量 {percent}%" + ("，现在是静音的" if muted else "")
        return ToolResult(
            tool=self.name,
            ok=True,
            output=spoken,
            # 设备名进审计不进回答：使用者问的是「多大声」，不是「哪只喇叭」。
            audit={
                "decision": "executed",
                "action": action,
                "level": percent,
                "muted": muted,
                "device": str(getattr(endpoint, "name", ""))[:60],
            },
        )


def _truthy(value: Any) -> bool:
    """`{"mute": "false"}` 里那个字符串是**假**的意思。

    模型写 JSON 时偶尔把布尔写成字符串，而 `bool("false")` 是 `True` —— 那会让「取消静音」
    变成「静音」，一个当场听得出来但完全说不通的结果。同一个坑在 `shell.run` 的
    `confirmed` 上抓过一次（`"no"` 是个真值字符串）。
    """
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "off", "否"}
    return bool(value)


def _fraction(value: Any, *, signed: bool = False) -> float | None:
    """0–1 的小数，或 0–100 的整数百分比。认不出来就返回 ``None``。

    两种写法都收是因为模型两种都会写（`{"level": 0.5}` 与 `{"level": 50}`），而把 50 当成
    「设成 5000%」再钳到 1.0 会让「调到一半」变成「调到最大」—— 一个当场吓人的结果。
    判据是**绝对值大于 1**：0.5 只可能是一半，50 只可能是百分之五十。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if abs(number) > 1:
        number = number / 100.0
    if not signed:
        number = abs(number)
    return max(-1.0, min(1.0, number))


__all__ = ["DEFAULT_STEP", "SystemVolumeTool"]
