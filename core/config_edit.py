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
_ASSIGN = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+)(?P<sep>[ \t]*=[ \t]*)(?P<rest>.*)$")
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
        name = match.group("key")
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
        key = f"{section}.{match.group('key')}"
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
        if not _BARE_KEY.match(name):
            raise ConfigEditError(f"not a bare TOML key: {name!r}")
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
            block.append(f"{name} = {render(value)}")
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
            key = match.group("key")
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
                f"{match.group('indent')}{key}{match.group('sep')}"
                f"{new_text}{spacer}{comment}".rstrip()
            )
            changed[f"{section}.{key}"] = {"from": old_text, "to": new_text}
        additions = []
        for name, value in pending.items():
            additions.append(f"{name} = {render(value)}")
            changed[f"{section}.{name}"] = {"from": None, "to": render(value)}
        if additions:
            lines[after + 1 : after + 1] = additions

    return _commit(target, eol.join(lines) + newline, validate=validate, changed=changed)


def editable_keys(path: str | Path, allow: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """``scan`` as a sorted list, optionally narrowed to an allow-list of keys."""
    found = scan(path)
    keys = list(found) if allow is None else [key for key in allow if key in found]
    return [found[key] for key in keys]


__all__ = ["ConfigEditError", "editable_keys", "render", "scan", "set_scalars", "set_section"]
