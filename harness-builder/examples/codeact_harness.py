"""Code-built CodeAct harness with a fake LLM and a FakeExecutor (no Docker).

The FakeExecutor returns a canned result, so this shows the wiring only. It is
NOT a sandbox and says nothing about real Docker behaviour.

Run from the harness-builder directory::

    uv run python examples/codeact_harness.py
"""

import tempfile

import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecResult
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.builtin.run_code import run_code


def main() -> None:
    fence = "`" * 3
    reply = f"Sum it.\n{fence}python\nFINAL(sum(range(5)))\n{fence}"
    executor = FakeExecutor([ExecResult(exit_code=0, final_set=True, final_value="10")])
    with tempfile.TemporaryDirectory() as trace_dir:
        harness = (
            HarnessBuilder("codeact-demo", Settings(model="openai/gpt-4o-mini"))
            .strategy("codeact")
            .tools(run_code())
            .permissions([Rule("run_code", "allow")])
            .executor(executor)  # a FakeExecutor: no Docker needed
            .llm(FakeLLMClient([LLMResponse(text=reply)]))
            .trace_dir(trace_dir)
            .build()
        )
        result = anyio.run(harness.run, "Sum 0..4")
    print(f"answer: {result.answer}")
    print(f"stop: {result.stop.reason}")
    req = executor.requests[0]
    print(f"executor request: {req.language} {req.timeout_s} network={req.network}")


if __name__ == "__main__":
    main()
