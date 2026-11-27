"""One bash command in the sandbox."""

from collections.abc import Callable, Coroutine
from typing import Any

from harness.sandbox.container import Container, ExecResult, SandboxError
from harness.sandbox.shell import run_bash
from harness.tools.result import result_dict


def make_bash(container: Container) -> Callable[..., Coroutine[Any, Any, dict]]:
    # The inner function's name and signature are what the model is shown.
    async def bash(command: str) -> dict:
        try:
            result = await run_bash(container, command)
        except SandboxError as error:
            result = ExecResult("", error=str(error))
        return result_dict(result)

    return bash
