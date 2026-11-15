"""Model client tests; subprocess.run is always patched, claude is never invoked."""
from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

from shared_context.model import (
    ClaudeHeadlessClient,
    ModelError,
    StubModelClient,
    build_command,
)


def _ok(stdout: str = "hi") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_headless_client_sets_guard_env():
    with mock.patch("shared_context.model.subprocess.run", return_value=_ok("out")) as run:
        assert ClaudeHeadlessClient().complete("p", timeout_s=5) == "out"
    kwargs = run.call_args.kwargs
    assert kwargs["env"]["SHARED_CONTEXT_CAPTURE"] == "1"
    assert kwargs["timeout"] == 5
    assert kwargs["input"] == "p"
    assert kwargs["capture_output"] is True
    assert not kwargs.get("shell")
    assert isinstance(run.call_args.args[0], list)


def test_custom_guard_env_and_parent_env_preserved(monkeypatch):
    monkeypatch.setenv("FOO_TEST", "bar")
    with mock.patch("shared_context.model.subprocess.run", return_value=_ok()) as run:
        ClaudeHeadlessClient(guard_env="MY_GUARD").complete("p", timeout_s=1)
    env = run.call_args.kwargs["env"]
    assert env["MY_GUARD"] == "1" and env["FOO_TEST"] == "bar"


def test_build_command_uses_print_mode_without_prompt():
    cmd = build_command("claude")
    assert cmd[0] == "claude" and "-p" in cmd
    assert "--no-session-persistence" in cmd
    assert "--" not in cmd


def test_large_prompt_goes_via_stdin_not_argv():
    prompt = "x" * 100_000
    with mock.patch("shared_context.model.subprocess.run", return_value=_ok()) as run:
        ClaudeHeadlessClient().complete(prompt, timeout_s=5)
    kwargs = run.call_args.kwargs
    assert kwargs["input"] == prompt
    assert "stdin" not in kwargs
    assert kwargs["encoding"] == "utf-8"
    assert all(len(a) < 300 for a in run.call_args.args[0])


def test_nonzero_exit_raises_model_error():
    bad = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    with mock.patch("shared_context.model.subprocess.run", return_value=bad):
        with pytest.raises(ModelError, match="boom"):
            ClaudeHeadlessClient().complete("p", timeout_s=1)


def test_timeout_raises_model_error():
    exc = subprocess.TimeoutExpired(cmd="claude", timeout=1)
    with mock.patch("shared_context.model.subprocess.run", side_effect=exc):
        with pytest.raises(ModelError, match="timed out"):
            ClaudeHeadlessClient().complete("p", timeout_s=1)


def test_missing_binary_raises_model_error():
    with mock.patch("shared_context.model.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ModelError, match="not found"):
            ClaudeHeadlessClient().complete("p", timeout_s=1)


def test_stub_returns_in_order_records_prompts_and_exhausts():
    stub = StubModelClient(["a", "b"])
    assert stub.complete("1", timeout_s=1) == "a"
    assert stub.complete("2", timeout_s=1) == "b"
    assert stub.prompts == ["1", "2"]
    with pytest.raises(ModelError, match="exhausted"):
        stub.complete("3", timeout_s=1)


@pytest.mark.skipif(
    os.environ.get("SHARED_CONTEXT_LIVE") != "1", reason="live claude test; set SHARED_CONTEXT_LIVE=1"
)
def test_live_claude_roundtrip():
    out = ClaudeHeadlessClient().complete("Reply with the single word: pong", timeout_s=120)
    assert out.strip()
