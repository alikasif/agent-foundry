"""Per-strategy test adapters for the conformance suite (designs.md 7.4).

Each adapter turns neutral intents (``CallTool``, ``Finish``, ``Garbage``) into
the strategy's native ``LLMResponse`` and may adjust the builder (``configure``)
with what that strategy needs, e.g. CodeAct's ``run_code`` tool and a
``FakeExecutor``. To add a strategy (T033: ``codeact``), write an adapter and
add it to ``STRATEGY_FACTORIES``; the shared cases pick it up unchanged.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.llm.types import LLMResponse


@dataclass(frozen=True)
class CallTool:
    """Intent: call tool ``name`` with ``args`` under call id ``id``."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = "c0"


@dataclass(frozen=True)
class Finish:
    """Intent: end the run with ``answer``."""

    answer: str


@dataclass(frozen=True)
class Garbage:
    """Intent: a malformed response the strategy must reject as ``format``."""


Intent = CallTool | Finish | Garbage


class StrategyAdapter(Protocol):
    """Strategy-specific encoding of intents plus builder configuration."""

    name: str

    def encode(self, *intents: Intent) -> LLMResponse:
        """Return the native response for one step made of ``intents``."""
        ...

    def configure(self, builder: HarnessBuilder) -> HarnessBuilder:
        """Add whatever the strategy needs (tools, executor) to ``builder``."""
        ...


def _single(intents: tuple[Intent, ...], kind: type) -> bool:
    """Return True when ``intents`` is exactly one intent of type ``kind``."""
    return len(intents) == 1 and isinstance(intents[0], kind)


def _calls(intents: tuple[Intent, ...]) -> list[CallTool]:
    """Return ``intents`` as tool-call intents, rejecting mixed steps."""
    calls = [i for i in intents if isinstance(i, CallTool)]
    if len(calls) != len(intents) or not calls:
        raise ValueError(f"a step is one Finish, one Garbage or CallTools: {intents}")
    return calls


class ReActAdapter:
    """Native function calls; final answers are call-free text."""

    name = "react"

    def encode(self, *intents: Intent) -> LLMResponse:
        """Encode one step for ``ReActStrategy``."""
        if _single(intents, Finish):
            finish = intents[0]
            assert isinstance(finish, Finish)
            return LLMResponse(text=finish.answer)
        if _single(intents, Garbage):
            # A call without a name is malformed (plain text would be final).
            return LLMResponse(tool_calls=[ToolCall(id="g0", name=" ")])
        return LLMResponse(
            tool_calls=[
                ToolCall(id=c.id, name=c.name, args=dict(c.args))
                for c in _calls(intents)
            ]
        )

    def configure(self, builder: HarnessBuilder) -> HarnessBuilder:
        """ReAct needs nothing extra."""
        return builder


class EchoAdapter:
    """``CALL <json>`` lines and ``FINAL <answer>`` text."""

    name = "echo"

    def encode(self, *intents: Intent) -> LLMResponse:
        """Encode one step for ``EchoStrategy``."""
        if _single(intents, Finish):
            finish = intents[0]
            assert isinstance(finish, Finish)
            return LLMResponse(text=f"FINAL {finish.answer}")
        if _single(intents, Garbage):
            return LLMResponse(text="I think I will do something now.")
        lines = [
            "CALL " + json.dumps({"id": c.id, "name": c.name, "args": c.args})
            for c in _calls(intents)
        ]
        return LLMResponse(text="\n".join(lines))

    def configure(self, builder: HarnessBuilder) -> HarnessBuilder:
        """Echo needs nothing extra."""
        return builder


STRATEGY_FACTORIES: dict[str, Callable[[], StrategyAdapter]] = {
    "react": ReActAdapter,
    "echo": EchoAdapter,
}
"""Strategy name -> adapter factory. T033 adds ``"codeact"`` here."""
