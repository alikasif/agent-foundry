import asyncio
from pathlib import Path

import pytest

from harness.sandbox.container import (
    Container,
    ContainerSpec,
    ExecResult,
    SandboxError,
)
from harness.tools.bash import tool


def call(
    monkeypatch: pytest.MonkeyPatch, folder: Path, outcome: ExecResult | BaseException
) -> dict:
    container = Container(ContainerSpec("image", folder))
    seen: list[tuple[Container, str]] = []

    async def run_bash(target: Container, command: str) -> ExecResult:
        seen.append((target, command))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(tool, "run_bash", run_bash)
    try:
        return asyncio.run(tool.make_bash(container)("ls"))
    finally:
        assert seen == [(container, "ls")]


def test_result_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reply = call(monkeypatch, tmp_path, ExecResult("a\n", exit_code=0))

    assert reply == {"output": "a\n", "exit_code": 0}


def test_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reply = call(monkeypatch, tmp_path, ExecResult("", timed_out=True))

    assert reply == {"output": "", "timed_out": True}


def test_sandbox_error_is_returned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reply = call(monkeypatch, tmp_path, SandboxError("container is gone"))

    assert reply == {"output": "", "error": "container is gone"}


def test_cancellation_passes_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(asyncio.CancelledError):
        call(monkeypatch, tmp_path, asyncio.CancelledError())
