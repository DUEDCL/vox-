"""``.env`` -> 进程环境。

这个文件存在的理由是「密钥只从环境变量读」这条红线遇上 Windows 的现实：把 token 设成
用户级环境变量意味着机器上每个进程都能拿到它。所以这里最要紧的两条断言是
**已存在的变量不被覆盖**（命令行赢过文件）和**返回值里只有变量名**（启动日志会被复制走）。

证据等级：AUTO。
"""

from __future__ import annotations

import pytest

from core.env_file import DEFAULT_ENV_FILE, load_env_file, parse_env_text


def test_key_value_lines_are_parsed():
    assert parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_comments_blank_lines_and_junk_are_skipped():
    """A typo should not stop the process from starting: a misspelled *name* already
    surfaces as "that key did not take effect", which is the shorter path."""
    text = "# comment\n\nGOOD=yes\nnot-an-assignment\n   \n=novalue\n"

    assert parse_env_text(text) == {"GOOD": "yes"}


@pytest.mark.parametrize(
    "line, value",
    [
        ('K="quoted"', "quoted"),
        ("K='single'", "single"),
        ("K=  spaced  ", "spaced"),
        ('K="', '"'),
        ("K=", ""),
    ],
)
def test_surrounding_quotes_and_padding_are_stripped(line, value):
    assert parse_env_text(line) == {"K": value}


def test_an_equals_sign_in_the_value_survives():
    """Base64 and JWTs end in ``=`` padding; splitting on every ``=`` would truncate
    exactly the kind of value this file exists to carry."""
    assert parse_env_text("T=abc=def==") == {"T": "abc=def=="}


def test_loading_sets_variables_and_returns_only_their_names(tmp_path, monkeypatch):
    """Names, not values: the caller prints this in a startup banner."""
    env = tmp_path / DEFAULT_ENV_FILE
    env.write_text("VOX_TEST_A=secret-one\nVOX_TEST_B=secret-two\n", encoding="utf-8")
    monkeypatch.delenv("VOX_TEST_A", raising=False)
    monkeypatch.delenv("VOX_TEST_B", raising=False)

    loaded = load_env_file(env)

    assert loaded == ["VOX_TEST_A", "VOX_TEST_B"]
    import os

    assert os.environ["VOX_TEST_A"] == "secret-one"
    assert "secret-one" not in " ".join(loaded)


def test_a_variable_already_in_the_environment_is_not_overwritten(tmp_path, monkeypatch):
    """An explicit ``export`` on the command line is the usual way to override one
    value for one run; letting the file win would make that action silently useless."""
    env = tmp_path / DEFAULT_ENV_FILE
    env.write_text("VOX_TEST_C=from-file\n", encoding="utf-8")
    monkeypatch.setenv("VOX_TEST_C", "from-shell")

    loaded = load_env_file(env)

    import os

    assert os.environ["VOX_TEST_C"] == "from-shell"
    assert loaded == []


def test_an_empty_existing_value_is_treated_as_absent(tmp_path, monkeypatch):
    """``SET VAR=`` on Windows leaves an empty string rather than removing it, and an
    empty credential is not a credential."""
    env = tmp_path / DEFAULT_ENV_FILE
    env.write_text("VOX_TEST_D=real\n", encoding="utf-8")
    monkeypatch.setenv("VOX_TEST_D", "")

    assert load_env_file(env) == ["VOX_TEST_D"]


def test_a_missing_file_is_not_an_error(tmp_path):
    """Most machines do not need one."""
    assert load_env_file(tmp_path / "nope.env") == []


def test_a_directory_where_the_file_should_be_is_not_an_error(tmp_path):
    (tmp_path / DEFAULT_ENV_FILE).mkdir()

    assert load_env_file(tmp_path / DEFAULT_ENV_FILE) == []
