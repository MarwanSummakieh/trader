"""config._env: docker compose env_file passes values verbatim, so the
parser must survive quotes, CRLF, inline comments and stray whitespace —
each of these previously crash-looped BOTH containers at startup."""

from config import _env


def test_plain_value(monkeypatch):
    monkeypatch.setenv("X", "10000")
    assert _env("X", "0") == "10000"


def test_missing_returns_default(monkeypatch):
    monkeypatch.delenv("X", raising=False)
    assert _env("X", "fallback") == "fallback"


def test_double_and_single_quotes_stripped(monkeypatch):
    monkeypatch.setenv("X", '"10000"')
    assert float(_env("X", "0")) == 10000.0
    monkeypatch.setenv("X", "'alpaca'")
    assert _env("X", "") == "alpaca"


def test_crlf_and_whitespace_stripped(monkeypatch):
    monkeypatch.setenv("X", "10000\r")          # Windows-edited .env
    assert float(_env("X", "0")) == 10000.0
    monkeypatch.setenv("X", "  alpaca  ")
    assert _env("X", "") == "alpaca"


def test_inline_comment_stripped(monkeypatch):
    monkeypatch.setenv("X", "8 # validated default")
    assert int(_env("X", "0")) == 8


def test_dead_key_artifact_stripped(monkeypatch):
    # the real NAS outage: a Nordic-keyboard dead key left 'MAX_POSITIONS=¨8'
    monkeypatch.setenv("X", "¨8")
    assert int(_env("X", "0")) == 8
    monkeypatch.setenv("X", "﻿10000")      # UTF-8 BOM
    assert float(_env("X", "0")) == 10000.0


def test_hash_without_space_is_kept(monkeypatch):
    # tokens may legitimately contain '#' — only ' #' starts a comment
    monkeypatch.setenv("X", "abc#def")
    assert _env("X", "") == "abc#def"


def test_empty_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("X", "")
    assert _env("X", "8") == "8"
    monkeypatch.setenv("X", '""')
    assert _env("X", "8") == "8"