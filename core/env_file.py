"""``.env`` 里的键值 -> 本进程环境。

## 为什么需要它

密钥只从环境变量读，这是红线。但在 Windows 上把一个 token 设成用户级环境变量
（``setx``）意味着这台机器上**每一个**进程都能拿到它，包括那些完全不该看见它的。
一个 gitignored 的文件 + 启动时加载，把作用域收窄到这一个进程树。

``.env`` 早就在 ``.gitignore`` 里，Claude Code 的 deny 列表里也有 ``Read(.env)``。
这个模块只把它读进 ``os.environ``，不把值往别处传、不记日志、不进事件流 —— 返回的是
**变量名**，调用方要打印就只能打印名字。

## 已存在的变量不覆盖

命令行里显式 ``export`` 的必须赢过文件里的：一次性覆盖是调试时最常用的动作，让文件
赢会让那个动作静默失效。

## 故意不支持的东西

没有 ``${VAR}`` 插值、没有 ``export`` 前缀、没有多行值。这个文件只有一个用途 ——
放几个 key —— 而每加一条语法就多一种「写了但没生效」的形状。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 约定位置，相对仓库根。
DEFAULT_ENV_FILE = ".env"


def workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=VALUE`` 的行 -> 字典。认不出的行安静跳过。

    跳过而不是报错：这个文件是手写的，一行笔误让整个进程起不来是过度反应；而一个
    拼错的**变量名**本来就会以「那个 key 没生效」的形式暴露出来，那条路更短。
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        row = line.strip()
        if not row or row.startswith("#") or "=" not in row:
            continue
        name, _, raw = row.partition("=")
        key = name.strip()
        if not key:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: str | Path | None = None) -> list[str]:
    """把 ``.env`` 读进 ``os.environ``，返回**新设进去的变量名**（排序后）。

    返回名字而不是数量：调用方要在启动日志里说「读到了哪几个」，而数量说不清是哪几个。
    值一个都不返回 —— 启动日志会被复制到 issue 里。

    文件不存在返回空列表，这不是错误：绝大多数机器不需要它。
    """
    file = Path(path) if path is not None else workspace_root() / DEFAULT_ENV_FILE
    if not file.is_file():
        return []
    try:
        text = file.read_text(encoding="utf-8")
    except OSError:
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        # 已经在环境里的不动 —— 命令行赢过文件。
        if os.environ.get(key):
            continue
        os.environ[key] = value
        applied.append(key)
    return sorted(applied)


def set_env_value(name: str, value: str, path: str | Path | None = None) -> None:
    """把一个变量写进 ``.env``，同名的那一行整行换掉，没有就追加。

    **只改那一行。** 整份重写会把用户手写的注释和排序抹掉，而这个文件是要给人看的 ——
    它记着「这个 key 是哪个服务商的」这类只存在于注释里的信息。

    调用方负责校验变量名：能往这个文件里写任意名字等于能设 ``PATH``、``PYTHONPATH``，
    那是代码执行而不是配置。见 ``core/console/routes.py`` 的白名单。
    """
    file = Path(path) if path is not None else workspace_root() / DEFAULT_ENV_FILE
    line = f"{name}={value}"
    if not file.is_file():
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(line + "\n", encoding="utf-8")
        return
    lines = file.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, row in enumerate(lines):
        stripped = row.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == name:
            lines[index] = line
            replaced = True
            break
    if not replaced:
        lines.append(line)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


__all__ = [
    "DEFAULT_ENV_FILE",
    "load_env_file",
    "parse_env_text",
    "set_env_value",
    "workspace_root",
]
