"""Editing a TOML config without losing its comments.

``tomllib`` reads TOML and the standard library has nothing that writes it. The
obvious fix -- serialise the parsed dict back out -- throws away every comment in
the file, and in this project the comments are the part that carries the
reasoning: ``config/speaker.toml`` explains why the threshold is 0.5 and that it
must be set from REAL-MIC measurements rather than by feel. A settings screen that
silently deletes that is a downgrade disguised as a feature.

So this edits **lines**, not values. Three constraints follow from that, and each
one is narrower than what a serialiser could do:

- **Only keys that already exist can be changed.** Adding a key means adding a
  line, and every loader in this project treats an unknown key as an error
  anyway (``load_tools_config``, ``load_voice_config``). "Add a setting" is a code
  change, not a settings-screen action. The one exception is ``set_section``, and
  it is an exception about a different kind of file: ``config/models.toml``'s
  tables are *data* (one per model profile), so creating one is a settings action.
  It has to be asked for by table name, so nothing reaches it by accident.
- **Only single-line scalars and inline arrays.** ``denied_names`` in
  ``config/tools.toml`` spans several lines; rewriting that safely means
  understanding TOML, which is the thing we are avoiding. Multi-line values are
  refused by name so the caller can say "edit the file" instead of corrupting it.
- **The file is validated before it is kept.** The new text goes to a temporary
  file, the caller's own loader parses it, and only a clean parse gets moved into
  place. A config that fails its own validator never reaches disk.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: ``key = value`` at the start of a line, capturing indent, name, separator and
#: the rest. Deliberately anchored: a ``key =`` inside a multi-line array is
#: indented content, not an assignment, and must not match.
#: 一行赋值。``key`` 收**裸键和带引号的键**两种。
#:
#: 带引号那一种不是补全性 —— `config/tools.toml` 的 `[apps.sites]` 键是中文（`"抖音"`、
#: `"B站"`），而 TOML 里那必须加引号。只认裸键的后果是这一整张表**改不了**，而使用者点名
#: 要求「web 界面能进行全范围的配置修改」。捕获组把引号留在 ``key`` 里，
#: ``_key_name`` 负责脱掉它 —— 重写那一行时要原样写回去，所以两个形态都得留着。
_ASSIGN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')"
    r"(?P<sep>[ \t]*=[ \t]*)(?P<rest>.*)$"
)
_TABLE = re.compile(r"^[ \t]*\[(?P<name>[^\[\]]+)\][ \t]*(?:#.*)?$")
#: ``[[agents]]`` -- an array of tables. Each occurrence is a new element, so the
#: section name gets an index (``agents[0]``) and two entries with the same key
#: name no longer collide. ``config/agents.toml`` is the reason this exists.
_ARRAY_TABLE = re.compile(r"^[ \t]*\[\[(?P<name>[^\[\]]+)\]\][ \t]*(?:#.*)?$")
#: ``agents[1].enabled`` -> ("agents", 1, "enabled")
_INDEXED = re.compile(r"^(?P<table>[^\[\]]+)\[(?P<index>\d+)\]$")
#: A bare key and a dotted table name, for ``set_section``. Quoted keys are out of
#: scope on purpose: this project's config files do not use them, and accepting
#: them here would mean quoting rules in the writer too.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key_name(raw: str) -> str:
    """一行赋值里捕获到的键 -> 它真正的名字。带引号的脱引号。

    ``_ASSIGN`` 刻意把引号留在捕获组里（重写那一行要原样写回去），所以每个**比较**键名的
    地方都得先过这一层。漏一处的症状是「`"抖音"` 那一行明明在，却被当成不存在而在下面插了
    一条重复的」—— 而 TOML 里重复键是解析错误，所以那一步会被 validate 挡住，
    表现是「保存失败」而不是「写坏了」。
    """
    text = str(raw)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return text


def _write_key(name: str) -> str:
    """一个键名 -> 写进文件的形式。裸键原样，其余加双引号并转义。

    非法的（含换行或控制字符）**直接拒**：那样的键会把文件写成一个解析不了的东西，
    而那比拒绝一次保存糟得多。
    """
    text = str(name)
    if _BARE_KEY.match(text):
        return text
    if any(char in text for char in "\n\r") or any(ord(char) < 0x20 for char in text):
        raise ConfigEditError(f"键里有换行或控制字符，写不进 TOML：{text!r}")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
_SECTION_NAME = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")


class ConfigEditError(RuntimeError):
    """An edit that would change the file's meaning in a way we cannot verify."""


def _section_of(line: str, counters: dict[str, int]) -> str | None:
    """The section a header line opens, indexing arrays of tables. ``None`` if not
    a header at all."""
    array = _ARRAY_TABLE.match(line)
    if array:
        name = array.group("name").strip()
        index = counters.get(name, -1) + 1
        counters[name] = index
        return f"{name}[{index}]"
    table = _TABLE.match(line)
    if table:
        return table.group("name").strip()
    return None


def _lookup(parsed: Mapping[str, Any], section: str, name: str) -> Any:
    """The parsed value behind ``section`` + ``name``, arrays of tables included."""
    indexed = _INDEXED.match(section)
    if indexed:
        table = parsed.get(indexed.group("table"))
        if not isinstance(table, Sequence):
            return None
        position = int(indexed.group("index"))
        if position >= len(table):
            return None
        element = table[position]
        return element.get(name) if isinstance(element, Mapping) else None
    current = parsed.get(section)
    return current.get(name) if isinstance(current, Mapping) else None


def _split_comment(rest: str) -> tuple[str, str]:
    """Separate a value from its trailing comment, respecting quotes.

    ``threshold = 0.5  # 不要凭感觉调`` must keep its comment, and
    ``device = "mic#2"`` must not be truncated at the ``#``.
    """
    quote: str | None = None
    escaped = False
    for index, char in enumerate(rest):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            return rest[:index].rstrip(), rest[index:]
    return rest.rstrip(), ""


def render(value: Any) -> str:
    """One TOML scalar or inline array of strings. Anything else is refused."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # ``repr`` keeps the value round-trippable; ``1.0`` must not become ``1``,
        # which would re-read as an int and fail the loader's type check.
        text = repr(float(value))
        return text if ("." in text or "e" in text or "n" in text) else f"{text}.0"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        if "\n" in value or "\r" in value:
            raise ConfigEditError("a config value must not contain newlines")
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigEditError("only arrays of strings can be edited")
            items.append(render(item))
        return "[" + ", ".join(items) + "]"
    raise ConfigEditError(f"unsupported value type: {type(value).__name__}")


def _is_multiline_value(text: str) -> bool:
    """Whether a value opens a bracket or a triple quote it does not close."""
    stripped = text.strip()
    if stripped.startswith(("'''", '"""')):
        return True
    if stripped.startswith("["):
        return stripped.count("[") != stripped.count("]")
    if stripped.startswith("{"):
        return stripped.count("{") != stripped.count("}")
    return not stripped


def scan(path: str | Path) -> dict[str, dict[str, Any]]:
    """Map ``"section.key"`` -> where it is and whether it can be edited.

    The console's settings screen reads this to decide what to render as an input
    and what to render as "edit the file". Returning the reason rather than just
    omitting the key is what lets it say why.
    """
    text = Path(path).read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    found: dict[str, dict[str, Any]] = {}
    section = ""
    counters: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        header = _section_of(line, counters)
        if header is not None:
            section = header
            continue
        match = _ASSIGN.match(line)
        if not match or not section:
            continue
        value, _comment = _split_comment(match.group("rest"))
        name = _key_name(match.group("key"))
        key = f"{section}.{name}"
        entry = {
            "key": key,
            "section": section,
            "name": name,
            "line": number,
            "value": _lookup(parsed, section, name),
            "editable": not _is_multiline_value(value),
        }
        if not entry["editable"]:
            entry["reason"] = "multi-line value: edit the file directly"
        found[key] = entry
    return found


def set_scalars(
    path: str | Path,
    updates: Mapping[str, Any],
    *,
    validate: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Change existing keys in place, keeping every comment and blank line.

    ``updates`` is keyed ``"section.key"``. ``validate`` is the config's own loader
    -- it is handed the temporary file and may raise; nothing is moved into place
    if it does. Returns what changed, old value and new, for the audit line.
    """
    target = Path(path)
    if not target.is_file():
        raise ConfigEditError(f"config file not found: {target}")
    if not updates:
        return {}
    original, eol, newline = _read_lines(target)
    lines = original

    pending = dict(updates)
    changed: dict[str, Any] = {}
    section = ""
    counters: dict[str, int] = {}
    for index, line in enumerate(lines):
        header = _section_of(line, counters)
        if header is not None:
            section = header
            continue
        match = _ASSIGN.match(line)
        if not match or not section:
            continue
        key = f"{section}.{_key_name(match.group('key'))}"
        if key not in pending:
            continue
        old_text, comment = _split_comment(match.group("rest"))
        if _is_multiline_value(old_text):
            raise ConfigEditError(f"{key} spans multiple lines; edit the file directly")
        new_text = render(pending.pop(key))
        spacer = "  " if comment else ""
        lines[index] = (
            f"{match.group('indent')}{match.group('key')}{match.group('sep')}"
            f"{new_text}{spacer}{comment}".rstrip()
        )
        changed[key] = {"from": old_text, "to": new_text}

    if pending:
        unknown = ", ".join(sorted(pending))
        raise ConfigEditError(
            f"no such key in {target.name}: {unknown} "
            "(adding a key is a code change, not a settings change)"
        )

    return _commit(target, eol.join(lines) + newline, validate=validate, changed=changed)


def _read_lines(target: Path) -> tuple[list[str], str, str]:
    """The file as lines, plus the line ending it uses and its trailing newline.

    Both have to be carried by hand. ``read_text`` normalises ``\\r\\n`` to ``\\n``
    and ``write_text`` translates back to the platform default, so an LF file edited
    on Windows comes out with **every** line ending rewritten. Git may well hide
    that (this repository has ``core.autocrlf=true``), which is exactly why it is
    worth guarding: what it actually breaks is the one automatable check a config
    writer has -- that a save which edits nothing leaves the file byte for byte. A
    writer claiming to change one line should not rewrite the whole file.
    ``splitlines`` also drops the trailing newline, so a POSIX-clean file must be
    given its last one back and a file without one must not be silently handed one.
    """
    raw = target.read_bytes()
    eol = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")
    return text.splitlines(), eol, (eol if text.endswith(("\n", "\r")) else "")


def _commit(
    target: Path,
    text: str,
    *,
    validate: Callable[[Path], Any] | None,
    changed: dict[str, Any],
) -> dict[str, Any]:
    """Write to a scratch file, parse it, validate it, then move it into place.

    All four steps or none: a config that fails its own loader never reaches the
    real path, so a rejected edit leaves the previous file byte-for-byte intact.
    """
    scratch = target.with_suffix(target.suffix + ".tmp")
    # ``newline=""`` because ``text`` already carries the file's own line endings;
    # without it the platform translation would rewrite every one of them.
    scratch.write_text(text, encoding="utf-8", newline="")
    try:
        tomllib.loads(text)
        if validate is not None:
            validate(scratch)
    except Exception as exc:
        scratch.unlink(missing_ok=True)
        raise ConfigEditError(f"rejected: {type(exc).__name__}: {exc}") from exc
    os.replace(scratch, target)
    return changed


def set_section(
    path: str | Path,
    section: str,
    values: Mapping[str, Any],
    *,
    validate: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Set keys inside one table, **inserting** the ones that are not there yet.

    ``set_scalars`` refuses a key the file does not already have, and for a
    schema-fixed config that is the right answer: in ``voice.toml`` or
    ``tools.toml`` an unknown key is what a typo looks like, and every loader
    treats it as an error. ``config/models.toml`` is a different kind of file --
    its tables *are* the data, one per model profile -- so adding
    ``[profiles.cloud.llm]`` with a ``base`` line is a settings action there, not a
    code change. This function exists for that second kind and takes the table name
    explicitly, so no caller can reach it by accident.

    What does not change: lines are never rewritten wholesale. An existing key
    keeps its comment, a new key is inserted after the last assignment in its
    table (so a comment block introducing the *next* table stays with it), a
    missing table is appended at the end, and the write is still
    validate-then-replace.
    """
    target = Path(path)
    if not target.is_file():
        raise ConfigEditError(f"config file not found: {target}")
    if "[" in section or "]" in section:
        raise ConfigEditError("set_section cannot create or edit an array of tables")
    if not _SECTION_NAME.match(section):
        raise ConfigEditError(f"not a plain table name: {section!r}")
    for name in values:
        # 不再要求裸键：`[apps.sites]` 的键是中文，而那在 TOML 里是合法的引号键。
        # 非法的（换行 / 控制字符）由 `_write_key` 拒。
        _write_key(name)
    if not values:
        return {}

    original, eol, newline = _read_lines(target)
    lines = original

    #: The table's own span: from its header to the next header, exclusive.
    start: int | None = None
    end = len(lines)
    counters: dict[str, int] = {}
    for index, line in enumerate(lines):
        header = _section_of(line, counters)
        if header is None:
            continue
        if start is None:
            if header == section:
                start = index
        else:
            end = index
            break

    pending = dict(values)
    changed: dict[str, Any] = {}

    if start is None:
        block = [] if (not lines or not lines[-1].strip()) else [""]
        block.append(f"[{section}]")
        for name, value in pending.items():
            block.append(f"{_write_key(name)} = {render(value)}")
            changed[f"{section}.{name}"] = {"from": None, "to": render(value)}
        lines.extend(block)
    else:
        # Default insertion point is the header itself: an empty table gets its
        # first key on the line below it.
        after = start
        for index in range(start + 1, end):
            match = _ASSIGN.match(lines[index])
            if not match:
                continue
            after = index
            key = _key_name(match.group("key"))
            if key not in pending:
                continue
            old_text, comment = _split_comment(match.group("rest"))
            if _is_multiline_value(old_text):
                raise ConfigEditError(
                    f"{section}.{key} spans multiple lines; edit the file directly"
                )
            new_text = render(pending.pop(key))
            spacer = "  " if comment else ""
            lines[index] = (
                f"{match.group('indent')}{match.group('key')}{match.group('sep')}"
                f"{new_text}{spacer}{comment}".rstrip()
            )
            changed[f"{section}.{key}"] = {"from": old_text, "to": new_text}
        additions = []
        for name, value in pending.items():
            additions.append(f"{_write_key(name)} = {render(value)}")
            changed[f"{section}.{name}"] = {"from": None, "to": render(value)}
        if additions:
            lines[after + 1 : after + 1] = additions

    return _commit(target, eol.join(lines) + newline, validate=validate, changed=changed)


def drop_key(
    path: str | Path,
    section: str,
    name: str,
    *,
    validate: Callable[[Path], Any] | None = None,
) -> bool:
    """删掉一张表里的一个键。返回是否真的删掉了。

    只删**一行**，而且只在指定的表里找 —— 一个能删任意行的编辑器在这个项目里没有用途，
    而它能造成的破坏（删掉一条安全边界的赋值）比它省下的事大得多。

    注释跟着走：删的是那一行，它上面的注释块**留下**。那通常是对的（一段介绍整张表的注释
    不该因为删掉表里一条而消失），代价是删干净一整条带说明的条目要手动收尾。
    """
    target = Path(path)
    if not target.is_file():
        raise ConfigEditError(f"config file not found: {target}")
    original, eol, newline = _read_lines(target)
    lines = list(original)
    counters: dict[str, int] = {}
    current: str | None = None
    hit: int | None = None
    for index, line in enumerate(lines):
        header = _section_of(line, counters)
        if header is not None:
            current = header
            continue
        if current != section:
            continue
        match = _ASSIGN.match(line)
        if match and _key_name(match.group("key")) == name:
            hit = index
            break
    if hit is None:
        return False
    del lines[hit]
    _commit(target, eol.join(lines) + newline, validate=validate, changed={f"{section}.{name}": None})
    return True


def editable_keys(path: str | Path, allow: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """``scan`` as a sorted list, optionally narrowed to an allow-list of keys."""
    found = scan(path)
    keys = list(found) if allow is None else [key for key in allow if key in found]
    return [found[key] for key in keys]


__all__ = [
    "ConfigEditError",
    "drop_key",
    "editable_keys",
    "render",
    "scan",
    "set_scalars",
    "set_section",
]
