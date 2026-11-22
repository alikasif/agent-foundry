"""Per-strategy test adapters for the conformance suite (designs.md 7.4).

Each adapter turns neutral intents (``CallTool``, ``Finish``, ``Garbage``) into
the strategy's native ``LLMResponse`` and may adjust the builder (``configure``)
with what that strategy needs, e.g. CodeAct's ``run_code`` tool and a
``FakeExecutor``. To add a strategy (T033: ``codeact``), write an adapter and
add it to ``STRATEGY_FACTORIES``; the shared cases pick it up unchanged.

An adapter also names the ``extra_rules`` its scaffolding needs (merged in
front of each case's rules) and which calls are ``scaffold``: calls the
encoding itself adds (CodeAct's ``FINAL()`` block) rather than calls a case
asked for, so the cases can leave them out of their assertions.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from harness_builder.core.builder import HarnessBuilder
from harness_builder.core.types import ToolCall
from harness_builder.executor.fake import FakeExecutor
from harness_builder.executor.models import ExecRequest, ExecResult
from harness_builder.llm.types import LLMResponse
from harness_builder.permissions.policy import Rule
from harness_builder.strategies.codeact.strategy import BLOCK_ID_PREFIX
from harness_builder.tools.builtin.run_code import run_code

RUN_CODE_ALLOW = Rule("run_code", "allow")
FINAL_LINE = re.compile(r"^FINAL\((?P<value>.*)\)\s*$", re.MULTILINE)


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
    extra_rules: tuple[Rule, ...]

    def encode(self, *intents: Intent) -> LLMResponse:
        """Return the native response for one step made of ``intents``."""
        ...

    def configure(self, builder: HarnessBuilder) -> HarnessBuilder:
        """Add whatever the strategy needs (tools, executor) to ``builder``."""
        ...

    def scaffold(self, call_id: str) -> bool:
        """Return True for a call added by the encoding, not by the case."""
        ...


class FinalExecutor(FakeExecutor):
    """``FakeExecutor`` that honours a ``FINAL(<json literal>)`` line in the code.

    Code without such a line exits 0 with no output. The value is decoded
    with ``json.loads`` so each case's answer comes from its own script.
    """

    async def run(self, req: ExecRequest) -> ExecResult:
        """Record ``req`` and return ``final_set`` when the code calls FINAL."""
        await super().run(req)
        match = FINAL_LINE.search(req.code)
        if match is None:
            return ExecResult(exit_code=0)
        value = json.loads(match.group("value"))
        return ExecResult(exit_code=0, final_set=True, final_value=str(value))


def final_block(answer: str) -> str:
    """Return a Python block that ends the run with ``answer`` via ``FINAL()``."""
    return f"```python\nFINAL({json.dumps(answer)})\n```"


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
    extra_rules: tuple[Rule, ...] = ()

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

    def scaffold(self, call_id: str) -> bool:
        """ReAct adds no calls of its own."""
        return False


class EchoAdapter:
    """``CALL <json>`` lines and ``FINAL <answer>`` text."""

    name = "echo"
    extra_rules: tuple[Rule, ...] = ()

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

    def scaffold(self, call_id: str) -> bool:
        """Echo adds no calls of its own."""
        return False


class CodeActAdapter:
    """Native calls for tools, a ``FINAL()`` block to finish (FR-004).

    ``Finish`` is a Python block calling ``FINAL(answer)``; it runs through
    ``run_code`` on a ``FinalExecutor`` (which sets ``final_set``), so every
    shared case also exercises the block -> run_code -> final path. Tool
    intents stay native calls, which CodeAct passes through (U11).
    ``Garbage`` is a block in an executable but unsupported language.
    """

    name = "codeact"
    extra_rules: tuple[Rule, ...] = (RUN_CODE_ALLOW,)

    def encode(self, *intents: Intent) -> LLMResponse:
        """Encode one step for ``CodeActStrategy``."""
        if _single(intents, Finish):
            finish = intents[0]
            assert isinstance(finish, Finish)
            return LLMResponse(text=f"Finishing.\n\n{final_block(finish.answer)}")
        if _single(intents, Garbage):
            return LLMResponse(text="```ruby\nputs 'hi'\n```")
        return LLMResponse(
            tool_calls=[
                ToolCall(id=c.id, name=c.name, args=dict(c.args))
                for c in _calls(intents)
            ]
        )

    def configure(self, builder: HarnessBuilder) -> HarnessBuilder:
        """Add ``run_code`` (required by CodeAct) and a ``FinalExecutor``."""
        return builder.tools(run_code()).executor(FinalExecutor())

    def scaffold(self, call_id: str) -> bool:
        """Block calls (the ``FINAL()`` block) are added by the encoding."""
        return call_id.startswith(BLOCK_ID_PREFIX)


STRATEGY_FACTORIES: dict[str, Callable[[], StrategyAdapter]] = {
    "react": ReActAdapter,
    "echo": EchoAdapter,
    "codeact": CodeActAdapter,
}
"""Strategy name -> adapter factory."""
