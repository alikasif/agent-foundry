"""Scripted LLM client for tests and smoke runs (designs.md 6). No network."""

from collections.abc import Callable

from harness_builder.core.types import Message, Usage
from harness_builder.errors import HarnessError
from harness_builder.llm.tokens import HeuristicCounter
from harness_builder.llm.types import LLMRequest, LLMResponse

Script = list[LLMResponse] | Callable[[LLMRequest], LLMResponse]


class FakeScriptExhaustedError(HarnessError):
    """A list-scripted ``FakeLLMClient`` was called more times than scripted.

    Not a transient error: the loop must not retry it.
    """


class FakeLLMClient:
    """``LLMClient`` that replays a script and records every request.

    ``script`` is either a list of responses returned in order, or a callable
    mapping each request to a response. A scripted response whose usage is the
    default ``Usage()`` gets deterministic token counts from
    ``HeuristicCounter``; explicit usage is kept as scripted. ``llm_calls`` is
    always 1 and ``cost_usd`` stays as scripted (the budget ledger prices it).
    """

    def __init__(self, script: Script) -> None:
        self._script = script
        self._counter = HeuristicCounter()
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        """Record ``req`` and return the next scripted response."""
        index = len(self.requests)
        self.requests.append(req)
        if callable(self._script):
            response = self._script(req)
        elif index < len(self._script):
            response = self._script[index]
        else:
            raise FakeScriptExhaustedError(
                f"FakeLLMClient script exhausted: call {index + 1} but only "
                f"{len(self._script)} response(s) scripted"
            )
        return response.model_copy(update={"usage": self._usage(req, response)})

    def _usage(self, req: LLMRequest, response: LLMResponse) -> Usage:
        if response.usage != Usage():
            return response.usage.model_copy(update={"llm_calls": 1})
        output = Message(
            role="assistant", content=response.text, tool_calls=response.tool_calls
        )
        input_tokens = self._counter.count(req.messages)
        if req.system:
            input_tokens += self._counter.count_text(req.system)
        return Usage(
            input_tokens=input_tokens,
            output_tokens=self._counter.count([output]),
            llm_calls=1,
        )
