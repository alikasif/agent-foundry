"""Code-built ReAct harness driven by a scripted fake LLM (no key, no network).

Run from the harness-builder directory::

    uv run python examples/react_harness.py
"""

import tempfile

import anyio

from harness_builder import HarnessBuilder, Settings
from harness_builder.core.types import ToolCall
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.tools.registry import tool


@tool
def add(a: int, b: int) -> str:
    """Add two integers."""
    return str(a + b)


def main() -> None:
    script = [
        LLMResponse(tool_calls=[ToolCall(id="c1", name="add", args={"a": 2, "b": 3})]),
        LLMResponse(text="2 + 3 = 5"),
    ]
    with tempfile.TemporaryDirectory() as trace_dir:
        harness = (
            HarnessBuilder("calc", Settings(model="openai/gpt-4o-mini"))
            .strategy("react")
            .system_prompt("You are a calculator.")
            .tools(add)
            .permissions([Rule("add", "allow")])  # unlisted tools are denied
            .budget(max_steps=5)
            .llm(FakeLLMClient(script))
            .trace_dir(trace_dir)
            .build()
        )
        result = anyio.run(harness.run, "What is 2 + 3?")
        print(f"answer: {result.answer}")
        print(f"stop: {result.stop.reason}")
        print(f"trace written: {result.trace_path is not None}")


if __name__ == "__main__":
    main()
