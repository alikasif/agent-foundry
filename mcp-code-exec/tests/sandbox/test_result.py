"""Tests for RunResult: what the model sees for one script run."""

from mcpx.sandbox.result import RunResult


def test_render_orders_stdout_error_notices() -> None:
    result = RunResult(
        stdout="out",
        error="Traceback: boom",
        timed_out=True,
        truncated=True,
        max_output_chars=100,
        timeout_s=2.5,
    )
    assert result.render() == (
        "out\nTraceback: boom\n"
        "[output truncated at 100 chars] [timed out after 2.5 s]\n"
    )


def test_render_omits_numbers_when_zero() -> None:
    result = RunResult(stdout="out\n", truncated=True, timed_out=True)
    assert result.render() == "out\n[output truncated] [timed out]\n"


def test_ok_is_false_on_error_or_timeout() -> None:
    assert RunResult(stdout="hi\n").ok
    assert not RunResult(stdout="", error="boom").ok
    assert not RunResult(stdout="", timed_out=True).ok
    assert RunResult(stdout="x", truncated=True).ok
