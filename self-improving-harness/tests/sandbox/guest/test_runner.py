import builtins
from pathlib import Path

import pytest

from harness.sandbox.guest.runner import execute, read_capped, truncate


def test_execute_keeps_namespace_and_echoes_final_expression(
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace: dict = {}

    assert execute("x = 1", namespace) is None
    assert execute("x + 1", namespace) is None

    assert capsys.readouterr().out == "2\n"


def test_execute_does_not_echo_none(capsys: pytest.CaptureFixture[str]) -> None:
    execute("print('hi')", {})

    assert capsys.readouterr().out == "hi\n"


def test_execute_returns_traceback_for_system_exit() -> None:
    error = execute("raise SystemExit(3)", {})

    assert error is not None
    assert "SystemExit" in error


def test_execute_returns_traceback_for_syntax_error() -> None:
    error = execute("def (:", {})

    assert error is not None
    assert "SyntaxError" in error


def test_execute_error_is_encodable_with_lone_surrogate() -> None:
    error = execute("raise ValueError(chr(0xDCFF))", {})

    assert error is not None
    assert "ValueError: ?" in error
    error.encode("utf-8")


def test_execute_undoes_replaced_builtins(
    capsys: pytest.CaptureFixture[str],
) -> None:
    saved = dict(vars(builtins))
    code = "import builtins\nbuiltins.print = builtins.repr = lambda *a, **k: ''\n42"
    try:
        error = execute(code, {})
        restored = builtins.print is saved["print"] and builtins.repr is saved["repr"]
    finally:
        vars(builtins).update(saved)

    assert error is None
    assert restored
    assert capsys.readouterr().out == "42\n"


def test_execute_reports_errors_after_builtins_are_replaced() -> None:
    saved = dict(vars(builtins))
    code = "import builtins\nbuiltins.isinstance = builtins.len = None\nraise KeyError"
    try:
        error = execute(code, {})
    finally:
        vars(builtins).update(saved)

    assert error is not None
    assert "KeyError" in error


def test_truncate_leaves_short_text_unchanged() -> None:
    assert truncate("abc", 3) == "abc"


def test_truncate_keeps_head_and_tail() -> None:
    result = truncate("a" * 50 + "b" * 50, 20)

    assert result == "a" * 10 + "\n... [80 characters cut] ...\n" + "b" * 10


def test_truncate_with_tiny_limit_does_not_return_whole_text() -> None:
    assert truncate("abcdef", 0) == "\n... [6 characters cut] ...\n"


def test_read_capped_bounds_large_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "output"
    path.write_bytes(b"\xff" * 10 + b"x" * 1_000_000)

    result = read_capped(str(path), 100)

    assert len(result) < 300
    assert result.startswith("�" * 10)
    assert result.endswith("x" * 50)
    assert "characters cut" in result


def test_read_capped_counts_characters_not_bytes(tmp_path: Path) -> None:
    path = tmp_path / "output"
    path.write_bytes("é".encode() * 10)

    assert read_capped(str(path), 10) == "é" * 10
