"""Line-level TOML editing: comments survive, unverifiable edits are refused.

The comments in ``config/*.toml`` carry the reasoning ("0.5 是常用起点，最终值应按
REAL-MIC 实测调定"). A settings screen that serialises the parsed dict back out
would delete all of it, so this module edits lines. These tests pin the three
things that makes it safe: nothing else on the line moves, a value that cannot be
verified is refused, and a file that fails its own loader never lands.

Evidence level: AUTO.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_edit import (
    ConfigEditError,
    editable_keys,
    render,
    scan,
    set_scalars,
    set_section,
)

SAMPLE = '''# 顶部说明，必须活下来
[speaker]
# 余弦相似度阈值。高了误拒本人，低了放进他人。
threshold = 0.5
require_verification = true  # 逃生阀，不要动
min_verify_seconds = 0.6

[capture]
device = "mic#2"
names = ["a", "b"]
multi = [
  "x",
  "y",
]
'''


@pytest.fixture
def sample(tmp_path) -> Path:
    path = tmp_path / "speaker.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


# --------------------------------------------------------------------- render


def test_render_covers_the_scalar_types():
    assert render(True) == "true"
    assert render(False) == "false"
    assert render(3) == "3"
    assert render(0.5) == "0.5"
    assert render("hi") == '"hi"'
    assert render(["a", "b"]) == '["a", "b"]'


def test_render_keeps_a_float_a_float():
    """``1.0`` must not become ``1``: it would re-read as an int and fail the
    loader's type check on the next start."""
    assert render(1.0) == "1.0"


def test_render_escapes_quotes_and_backslashes():
    assert render('say "hi"') == '"say \\"hi\\""'
    assert render("C:\\models") == '"C:\\\\models"'


def test_render_refuses_a_newline():
    with pytest.raises(ConfigEditError, match="newlines"):
        render("line1\nline2")


def test_render_refuses_a_non_string_array():
    with pytest.raises(ConfigEditError, match="arrays of strings"):
        render([1, 2])


def test_render_refuses_an_unsupported_type():
    with pytest.raises(ConfigEditError, match="unsupported value type"):
        render({"a": 1})


# ----------------------------------------------------------------------- scan


def test_scan_finds_keys_with_their_current_values(sample):
    found = scan(sample)
    assert found["speaker.threshold"]["value"] == 0.5
    assert found["speaker.require_verification"]["value"] is True
    assert found["capture.device"]["value"] == "mic#2"


def test_scan_marks_a_multiline_array_as_not_editable(sample):
    entry = scan(sample)["capture.multi"]
    assert entry["editable"] is False
    assert "edit the file" in entry["reason"]


def test_scan_marks_an_inline_array_as_editable(sample):
    assert scan(sample)["capture.names"]["editable"] is True


def test_scan_does_not_mistake_array_contents_for_assignments(sample):
    """The indented ``"x",`` lines inside ``multi`` are content, not keys."""
    assert not any(key.endswith(".x") for key in scan(sample))


def test_editable_keys_can_be_narrowed_to_an_allow_list(sample):
    keys = editable_keys(sample, allow=["speaker.threshold", "nope.gone"])
    assert [entry["key"] for entry in keys] == ["speaker.threshold"]


# ----------------------------------------------------------------- set_scalars


def test_editing_keeps_every_comment_and_blank_line(sample):
    set_scalars(sample, {"speaker.threshold": 0.62})
    text = sample.read_text(encoding="utf-8")
    assert "# 顶部说明，必须活下来" in text
    assert "# 余弦相似度阈值。高了误拒本人，低了放进他人。" in text
    assert "threshold = 0.62" in text


def test_a_trailing_comment_survives_its_line_being_edited(sample):
    set_scalars(sample, {"speaker.require_verification": False})
    assert "require_verification = false  # 逃生阀，不要动" in sample.read_text(encoding="utf-8")


def test_only_the_target_line_changes(sample):
    before = sample.read_text(encoding="utf-8").splitlines()
    set_scalars(sample, {"speaker.threshold": 0.7})
    after = sample.read_text(encoding="utf-8").splitlines()
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1
    assert len(before) == len(after)


def test_several_keys_in_one_pass(sample):
    changed = set_scalars(
        sample, {"speaker.threshold": 0.8, "capture.device": "3", "capture.names": ["c"]}
    )
    assert set(changed) == {"speaker.threshold", "capture.device", "capture.names"}
    text = sample.read_text(encoding="utf-8")
    assert 'device = "3"' in text and 'names = ["c"]' in text


def test_the_report_says_what_changed(sample):
    changed = set_scalars(sample, {"speaker.threshold": 0.9})
    assert changed["speaker.threshold"] == {"from": "0.5", "to": "0.9"}


def test_a_hash_inside_a_string_is_not_treated_as_a_comment(sample):
    """``device = "mic#2"`` must not be truncated at the ``#``."""
    set_scalars(sample, {"capture.device": "mic#7"})
    assert 'device = "mic#7"' in sample.read_text(encoding="utf-8")


def test_an_unknown_key_is_refused_and_nothing_is_written(sample):
    before = sample.read_text(encoding="utf-8")
    with pytest.raises(ConfigEditError, match="no such key"):
        set_scalars(sample, {"speaker.threshold": 0.9, "speaker.nonsense": 1})
    assert sample.read_text(encoding="utf-8") == before


def test_a_multiline_value_is_refused_by_name(sample):
    with pytest.raises(ConfigEditError, match="spans multiple lines"):
        set_scalars(sample, {"capture.multi": ["z"]})


def test_no_updates_is_a_no_op(sample):
    before = sample.read_text(encoding="utf-8")
    assert set_scalars(sample, {}) == {}
    assert sample.read_text(encoding="utf-8") == before


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ConfigEditError, match="not found"):
        set_scalars(tmp_path / "absent.toml", {"a.b": 1})


def test_a_validator_that_raises_leaves_the_file_untouched(sample):
    before = sample.read_text(encoding="utf-8")

    def picky(path: Path) -> None:
        raise ValueError("threshold must stay below 0.9")

    with pytest.raises(ConfigEditError, match="rejected: ValueError"):
        set_scalars(sample, {"speaker.threshold": 0.95}, validate=picky)

    assert sample.read_text(encoding="utf-8") == before
    assert not list(sample.parent.glob("*.tmp")), "the scratch file is cleaned up"


def test_a_validator_that_passes_lets_the_edit_land(sample):
    seen: list[Path] = []
    set_scalars(sample, {"speaker.threshold": 0.55}, validate=seen.append)
    assert seen and seen[0].suffix == ".tmp", "the validator sees the candidate, not the original"
    assert "threshold = 0.55" in sample.read_text(encoding="utf-8")


def test_the_trailing_newline_is_preserved(sample):
    set_scalars(sample, {"speaker.threshold": 0.51})
    assert sample.read_text(encoding="utf-8").endswith("\n")


def test_a_file_without_a_trailing_newline_does_not_gain_one(tmp_path):
    path = tmp_path / "t.toml"
    path.write_text("[a]\nb = 1", encoding="utf-8")
    set_scalars(path, {"a.b": 2})
    assert path.read_text(encoding="utf-8") == "[a]\nb = 2"


# ------------------------------------------------------------- line endings


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_an_edit_keeps_the_files_own_line_endings(tmp_path, eol):
    """A save that edits nothing must leave the file byte for byte.

    ``read_text`` normalises CRLF away and ``write_text`` translates back to the
    platform default, so without explicit handling one console save rewrites every
    line ending in the file. Git hides it here (``core.autocrlf=true``); what it
    breaks is the hash check that is the only automatable forensic for a config
    writer -- an unchanged save of ``config/models.toml`` moved 62 line endings and
    changed the file's SHA-256.
    """
    path = tmp_path / "voice.toml"
    source = "# 为什么是 2\n[wake]\nkeywords_threshold = 0.25\nnum_threads = 2\n"
    path.write_bytes(source.replace("\n", eol).encode("utf-8"))
    set_scalars(path, {"wake.num_threads": 4})
    raw = path.read_bytes()
    assert raw.count(eol.encode()) == 4
    if eol == "\n":
        assert b"\r" not in raw
    else:
        assert raw.count(b"\n") == raw.count(b"\r\n"), "no mixed endings"


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_an_inserted_key_uses_the_files_own_line_endings(tmp_path, eol):
    path = tmp_path / "models.toml"
    path.write_bytes(('[profiles.a.llm]\nprovider = "x"\n'.replace("\n", eol)).encode("utf-8"))
    set_section(path, "profiles.a.llm", {"model": "m-1"})
    raw = path.read_bytes()
    assert raw.count(eol.encode()) == 3
    if eol == "\n":
        assert b"\r" not in raw


# ------------------------------------------------- against the shipped configs


@pytest.mark.parametrize("name", ["voice.toml", "speaker.toml", "tools.toml", "memory.toml"])
def test_the_shipped_configs_are_scannable(name):
    """Every config the console offers to edit must be readable by this scanner.

    Without this, a config file could grow a shape the scanner mis-reads and the
    first symptom would be a settings screen quietly showing the wrong values.
    """
    path = Path(__file__).resolve().parents[1] / "config" / name
    found = scan(path)
    assert found, f"{name} produced no keys"
    for entry in found.values():
        assert entry["line"] > 0
        assert entry["section"]
