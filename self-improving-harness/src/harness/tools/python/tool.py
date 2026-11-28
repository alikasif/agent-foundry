"""Python in the sandbox's persistent interpreter."""

from collections.abc import Callable, Coroutine
from typing import Any

from harness.sandbox.container import ExecResult, SandboxError
from harness.sandbox.interpreter import Interpreter
from harness.tools.result import result_dict


def make_python(interpreter: Interpreter) -> Callable[..., Coroutine[Any, Any, dict]]:
    # The inner function's name and signature are what the model is shown.
    async def python(code: str) -> dict:
        try:
            result = await interpreter.run(code)
        except SandboxError as error:
            result = ExecResult("", error=str(error))
        return result_dict(result)

    return python
