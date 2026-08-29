"""``config/models.toml``: the model profiles, with a loader strict enough to trust.

A profile is one ASR + TTS + LLM triple; ``active`` names the one in effect. The
console's 模型配置 module is the only writer today, and a future LLM adapter will be
the reader -- which is why this lives next to ``config_edit`` rather than inside
``core/console/``.

Three things this file refuses, each for a reason the project has already paid for:

- **Unknown keys raise.** Same stance as ``load_voice_config`` and
  ``load_tools_config``: a misspelled ``key_env`` that is silently ignored leaves an
  operator believing a setting applied when it did not.
- **A value that looks like a credential is refused whole.** The header of
  ``config/models.toml`` promises this. Only the *name* of an environment variable
  belongs here; the value is read from the environment at call time. A key written
  into this file would reach the version history, the logs and the event stream.
- **Plain ``http://`` only for loopback, and never credentials in a URL.** The same
  rule ``core/session_bridge.py``, ``core/agents/http.py`` and
  ``core/tools/search_backends.py`` each enforce for their own endpoints. This is a
  **fourth copy**, for the reason the third one already wrote down: each of the
  others is a security boundary with its own exception type and pinned messages, so
  extracting a shared helper means editing three tested security modules to serve a
  new caller. The extraction is worth doing on its own; ``docs/backlog.md`` carries
  it, now with four call sites instead of three.

Nothing in this module reaches the network. Probing an endpoint is the console's
job (``ConsoleApi.models_probe``) and is a deliberate, per-click action.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from core.audio.config import repo_root
from core.config_edit import ConfigEditError, set_section

DEFAULT_CONFIG_NAME = "models.toml"

#: The three model roles a profile configures. Fixed: they are the three the voice
#: stack assembles, not an open set.
KINDS: tuple[str, ...] = ("asr", "tts", "llm")

#: Every key a ``[profiles.NAME.KIND]`` table may carry. This doubles as the
#: console's write allow-list -- there is no second list to keep in sync.
FIELDS: tuple[str, ...] = ("provider", "model", "base", "proto", "key_env")

#: Request shapes an adapter can speak. ``custom`` means "a human will wire it".
PROTOS: tuple[str, ...] = ("openai", "anthropic", "ollama", "custom")

#: A profile name becomes a TOML table name, so it is restricted to bare-key
#: characters. No dots: a dot would nest the table somewhere else entirely.
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")

#: An environment variable name, which is all ``key_env`` may ever hold.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: Prefixes that are unambiguously the front of a real credential. Matching one is
#: reported as a refusal, not stripped: a redacted secret still leaves the secret
#: in the file's history, and the operator needs to know it was rejected.
_SECRET_PREFIXES: tuple[str, ...] = (
    "sk-", "sk_", "rk-", "pk-", "ghp_", "gho_", "ghs_", "github_pat_",
    "xoxb-", "xoxp-", "xapp-", "AKIA", "ASIA", "AIza", "ya29.", "hf_", "glpat-",
)

#: A blob with no separators that mixes cases and digits: the shape of a token, not
#: of a model name (``qwen2.5:7b``), a slug (``deepseek``) or a URL (it has ``/``).
_BLOB = re.compile(r"^[A-Za-z0-9+/=_-]{24,}$")


class ModelsConfigError(RuntimeError):
    """A models config that cannot be trusted to mean what it says."""


def models_config_path(path: str | Path | None = None) -> Path:
    """Explicit argument, then ``VOX_MODELS_CONFIG``, then ``config/models.toml``."""
    if path is not None:
        return Path(path)
    return Path(os.getenv("VOX_MODELS_CONFIG") or repo_root() / "config" / DEFAULT_CONFIG_NAME)


def looks_like_secret(value: str) -> bool:
    """Whether a string is shaped like a credential rather than a setting.

    Narrow on purpose. ``model = "claude-opus-4-20250514"`` has no upper case,
    ``base = "https://api.deepseek.com/v1"`` has separators, and a slug is short --
    so none of them trip the blob rule, while a 40-character mixed-case token does.
    """
    text = value.strip()
    if not text:
        return False
    if text.startswith(_SECRET_PREFIXES):
        return True
    if "bearer " in text.lower():
        return True
    if not _BLOB.match(text):
        return False
    return (
        any(c.islower() for c in text)
        and any(c.isupper() for c in text)
        and any(c.isdigit() for c in text)
    )


def url_problem(url: str) -> str | None:
    """``None`` when ``url`` is an endpoint we are willing to touch.

    Same three checks the bridge and the HTTP agent make: absolute HTTP(S), no
    credentials in the URL, and plain HTTP only when the host is loopback.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "端点必须是完整的 http(s) URL"
    if parsed.username or parsed.password:
        return "端点里不许带凭据（user:pass@）"
    if parsed.scheme == "https":
        return None
    host = parsed.hostname.lower()
    if host == "localhost":
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return "明文 HTTP 只许回环地址"


def check_field(where: str, name: str, value: Any) -> str:
    """One field, validated. Returns the trimmed value; raises ``ModelsConfigError``.

    ``where`` is only used to build the message, so the caller decides how precise
    it is: ``profiles.local.llm`` when a file is being read, the same when one is
    being written.
    """
    label = f"{where}.{name}"
    if name not in FIELDS:
        raise ModelsConfigError(f"unknown model key: {label}")
    if not isinstance(value, str):
        raise ModelsConfigError(f"{label} must be a string")
    text = value.strip()
    if looks_like_secret(text):
        raise ModelsConfigError(
            f"{label} 看起来是一个密钥。这个文件里只写环境变量名（key_env），"
            "值从环境变量读 —— 写进来的 key 会进版本库、日志和事件流"
        )
    if name == "proto" and text and text not in PROTOS:
        raise ModelsConfigError(f"{label} must be one of: {', '.join(PROTOS)}")
    if name == "key_env" and text and not _ENV_NAME.match(text):
        raise ModelsConfigError(
            f"{label} 要一个环境变量名（大写字母、数字、下划线），不是密钥本身"
        )
    if name == "base" and text:
        problem = url_problem(text)
        if problem:
            raise ModelsConfigError(f"{label}: {problem}")
    return text


def load_models_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read and validate the profiles. A missing file yields an empty registry.

    Returns ``{"active": str, "profiles": {name: {"label": str, kind: {...}}}}``.
    An ``active`` that names no profile raises: a registry that points at a
    profile which is not there is exactly the kind of config that looks fine and
    is not.
    """
    config_path = models_config_path(path)
    if not config_path.is_file():
        return {"active": "", "profiles": {}}
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelsConfigError(f"models config is unreadable: {exc}") from exc

    unknown = sorted(set(raw) - {"active", "profiles"})
    if unknown:
        raise ModelsConfigError(f"unknown top-level key: {', '.join(unknown)}")

    active = raw.get("active", "")
    if not isinstance(active, str):
        raise ModelsConfigError("active must be a string")

    table = raw.get("profiles", {})
    if not isinstance(table, dict):
        raise ModelsConfigError("[profiles] must be a table")

    profiles: dict[str, Any] = {}
    for name, body in table.items():
        if not PROFILE_NAME.match(name):
            raise ModelsConfigError(f"not a usable profile name: {name!r}")
        if not isinstance(body, dict):
            raise ModelsConfigError(f"[profiles.{name}] must be a table")
        extra = sorted(set(body) - {"label", *KINDS})
        if extra:
            raise ModelsConfigError(f"unknown key in [profiles.{name}]: {', '.join(extra)}")
        label = body.get("label", "")
        if not isinstance(label, str):
            raise ModelsConfigError(f"profiles.{name}.label must be a string")
        entry: dict[str, Any] = {"label": label}
        for kind in KINDS:
            section = body.get(kind)
            if section is None:
                continue
            if not isinstance(section, dict):
                raise ModelsConfigError(f"[profiles.{name}.{kind}] must be a table")
            entry[kind] = {
                key: check_field(f"profiles.{name}.{kind}", key, value)
                for key, value in section.items()
            }
        profiles[name] = entry

    if active and active not in profiles:
        raise ModelsConfigError(f"active = {active!r} names no profile in this file")
    return {"active": active, "profiles": profiles}


def write_profile_kind(
    profile: str,
    kind: str,
    fields: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    label: str = "",
    preset: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write one profile's one role, creating the table when it is new.

    Empty strings are dropped rather than written: an empty ``base`` means "the
    preset's endpoint", and writing ``base = ""`` into the file would turn that
    into "no endpoint at all".

    ``preset`` is what the caller's provider table already says for this provider
    (``base`` / ``proto`` / ``key_env``). A field whose value merely repeats the
    preset **and is not already in the file** is not written: the file would then
    carry a second copy of an endpoint that lives in the provider table, and a
    later correction to that table would not reach it. A field that *is* already in
    the file keeps being written, so switching a profile back to a preset updates
    the line instead of leaving a stale override behind. Pass ``None`` (as the
    caller does for ``custom``) to persist everything.

    Validation runs twice on purpose -- once on the fields here, once by the loader
    against the whole candidate file -- because the second one is what catches an
    edit that is fine alone and wrong in context.
    """
    config_path = models_config_path(path)
    if not config_path.is_file():
        raise ModelsConfigError(f"config file not found: {config_path}")
    if not PROFILE_NAME.match(profile):
        raise ModelsConfigError(
            "方案名只许字母、数字、下划线和连字符（它会成为 TOML 的表名）"
        )
    if kind not in KINDS:
        raise ModelsConfigError(f"kind must be one of: {', '.join(KINDS)}")
    if not isinstance(label, str):
        raise ModelsConfigError("label must be a string")
    if looks_like_secret(label):
        raise ModelsConfigError("label 看起来是一个密钥；标签是给人看的名字")

    # Pre-flight. A file that is already invalid must say so about itself, not
    # about the candidate -- otherwise changing ``provider`` reports an error about
    # ``active`` and reads like the edit caused it.
    existing = load_models_config(config_path)
    known = existing["profiles"].get(profile)
    already = set((known or {}).get(kind, {}))

    where = f"profiles.{profile}.{kind}"
    defaults = dict(preset or {})
    values: dict[str, Any] = {}
    considered = 0
    for name, value in fields.items():
        checked = check_field(where, name, value)
        if not checked:
            continue
        considered += 1
        if name in defaults and defaults[name] == checked and name not in already:
            continue
        values[name] = checked
    if not considered:
        raise ModelsConfigError("没有要写的字段（空值不写入，那会把「用预设端点」变成「没有端点」）")

    changed: dict[str, Any] = {}
    if label and (known is None or known.get("label", "") != label):
        changed.update(
            set_section(config_path, f"profiles.{profile}", {"label": label}, validate=_validate)
        )
    if values:
        changed.update(set_section(config_path, where, values, validate=_validate))
    return changed


def _validate(candidate: Path) -> None:
    """The loader, as ``config_edit`` wants it: raises on a bad candidate file."""
    try:
        load_models_config(candidate)
    except ModelsConfigError as exc:
        raise ConfigEditError(str(exc)) from exc


__all__ = [
    "FIELDS",
    "KINDS",
    "PROFILE_NAME",
    "PROTOS",
    "ModelsConfigError",
    "check_field",
    "load_models_config",
    "looks_like_secret",
    "models_config_path",
    "url_problem",
    "write_profile_kind",
]
