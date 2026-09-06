"""``config/channels.toml`` -> 通道配置。**默认全关，不配就一个字节都不出网。**

和 `core/audio/config.py` 同一条规矩：未知的键**报错而不是被忽略**。一个拼错的
`enabled` 会让「我开了微信但它没反应」变成一次无从下手的排查，而报错直接指出那一行。

**这里没有放密钥的键。** token 只从环境变量读，文件里写的是变量名（`token_env`）——
和 agents / tts 完全一致。写 `token = "..."` 会因为「未知键」被拒。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "channels.toml"


class ChannelConfigError(RuntimeError):
    """一份不能被信任去表达它字面意思的通道配置。"""


#: 每个 ``[section]`` 允许的键 -> 默认值。形状本身就是校验器。
_SCHEMA: dict[str, dict[str, Any]] = {
    "weixin": {
        # **默认关。** 打开它意味着长轮询腾讯的端点、从微信 CDN 收发媒体 —— 那是出网，
        # 而出网必须是一次显式的选择。
        "enabled": False,
        # 去读哪个环境变量拿 iLink bot token。**名字，不是值。**
        "token_env": "VOX_WEIXIN_TOKEN",
        # 回复带不带语音。默认带（「在微信也能进行语音消息的处理和发送」），
        # 而文字**永远都带** —— 一条只有语音的回复在电脑上看不了也搜不到。
        "reply_with_voice": True,
        # 出站语音走原生语音气泡还是文件附件。**默认文件附件**：原生那条在上游
        # （Hermes 的 `send_voice`）注释里明写「not proven-working」，而一条发不出去的
        # 语音比一条能播的附件差。想试就打开它，那是 REAL-WEIXIN。
        "voice_native": False,
        # 入站语音优先用本机 ASR（拿得到原件且格式解得开时）。关掉就一律用腾讯自带的
        # STT 文本 —— 那份对非中文是错的（Hermes issue #27300）。
        "local_asr": True,
        # 长轮询挂多久。35 秒是上游用的值。
        "poll_timeout_s": 35.0,
    },
}


def config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.getenv("VOX_CHANNELS_CONFIG", "").strip()
    if override:
        return Path(override)
    from core.audio.config import repo_root

    return repo_root() / "config" / DEFAULT_CONFIG_NAME


def defaults() -> dict[str, Any]:
    return {
        f"{section}.{key}": value
        for section, keys in _SCHEMA.items()
        for key, value in keys.items()
    }


def _coerce(where: str, value: Any, default: Any) -> Any:
    """按默认值的类型检查。``bool`` 先判 —— Python 里 ``True`` 也是 ``int``，
    只用 isinstance 会让 ``enabled = 1`` 过关。"""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ChannelConfigError(f"{where} 要一个布尔值")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ChannelConfigError(f"{where} 要一个数")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ChannelConfigError(f"{where} 要一个字符串")
        return value
    raise ChannelConfigError(f"{where} 的类型不受支持")


def load_channels_config(path: str | Path | None = None) -> dict[str, Any]:
    """读配置，键拍平成 ``"section.key"``。文件不在就是出厂默认（全关）。"""
    resolved = config_path(path)
    merged = defaults()
    if not resolved.is_file():
        return merged
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ChannelConfigError(f"通道配置读不了：{exc}") from exc
    for section, values in raw.items():
        if section not in _SCHEMA:
            raise ChannelConfigError(f"没有这个通道：[{section}]")
        if not isinstance(values, dict):
            raise ChannelConfigError(f"[{section}] 得是一张表")
        for key, value in values.items():
            if key not in _SCHEMA[section]:
                raise ChannelConfigError(f"未知的键：{section}.{key}")
            merged[f"{section}.{key}"] = _coerce(
                f"{section}.{key}", value, _SCHEMA[section][key]
            )
    return merged


def open_weixin(config: dict[str, Any] | None = None, **overrides: Any) -> Any:
    """按配置建微信通道。**关着就返回 ``None``** —— 调用方据此决定要不要起那条线程。"""
    resolved = config if config is not None else load_channels_config()
    if not bool(resolved.get("weixin.enabled", False)):
        return None
    from core.channels.weixin import WeixinChannel

    return WeixinChannel(
        token_env=str(resolved.get("weixin.token_env") or "VOX_WEIXIN_TOKEN"),
        voice_native=bool(resolved.get("weixin.voice_native", False)),
        **overrides,
    )


__all__ = [
    "ChannelConfigError",
    "DEFAULT_CONFIG_NAME",
    "config_path",
    "defaults",
    "load_channels_config",
    "open_weixin",
]
