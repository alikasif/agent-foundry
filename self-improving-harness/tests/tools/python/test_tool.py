import asyncio
from pathlib import Path

import pytest

from harness.sandbox.container import (
    Container,
    ContainerSpec,
    ExecResult,
    SandboxError,
)
from harness.sandbox.interpreter import Interpreter
from harness.tools.python.tool import make_python


def call(
    monkeypatch: pytest.MonkeyPatch, folder: Path, outcome: ExecResult | BaseException
) -> dict:
    interpreter = Interpreter(Container(ContainerSpec("image", folder)))
    seen: list[str] = []

    async def run(code: str) -> ExecResult:
        seen.append(code)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(interpreter, "run", run)
    try:
        return asyncio.run(make_python(interpreter)("x = 1"))
    finally:
        assert seen == ["x = 1"]


def test_result_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert call(monkeypatch, tmp_path, ExecResult("1\n")) == {"output": "1\n"}


def test_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    outcome = ExecResult("", error="died", restarted=True)

    assert call(monkeypatch, tmp_path, outcome) == {
        "output": "",
        "error": "died",
        "restarted": True,
    }


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
