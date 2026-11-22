"""FakeLLMClient: scripted responses, request recording, deterministic usage."""

import math

import pytest

from harness_builder.core.protocols import LLMClient
from harness_builder.core.types import Message, ModelConfig, ToolCall, Usage
from harness_builder.errors import HarnessError
from harness_builder.llm.fake import FakeLLMClient, FakeScriptExhaustedError
from harness_builder.llm.types import LLMRequest, LLMResponse

pytestmark = pytest.mark.anyio


def make_request(content: str = "abcdefgh", system: str | None = None) -> LLMRequest:
    """Return a one-message request (8 chars of content by default)."""
    return LLMRequest(
        model=ModelConfig(name="fake"),
        system=system,
        messages=[Message(role="user", content=content)],
    )


def test_fake_client_satisfies_llm_client_protocol() -> None:
    client: LLMClient = FakeLLMClient([])
    assert client is not None


async def test_list_script_returns_responses_in_order() -> None:
    client = FakeLLMClient([LLMResponse(text="one"), LLMResponse(text="two")])
    first = await client.complete(make_request())
    second = await client.complete(make_request())
    assert (first.text, second.text) == ("one", "two")


async def test_requests_are_recorded() -> None:
    client = FakeLLMClient([LLMResponse(text="a"), LLMResponse(text="b")])
    r1, r2 = make_request("x"), make_request("y")
    await client.complete(r1)
    await client.complete(r2)
    assert client.requests == [r1, r2]


async def test_exhausted_script_raises_harness_error() -> None:
    client = FakeLLMClient([LLMResponse(text="only")])
    await client.complete(make_request())
    with pytest.raises(FakeScriptExhaustedError, match="exhausted"):
        await client.complete(make_request())
    assert issubclass(FakeScriptExhaustedError, HarnessError)
    assert len(client.requests) == 2


async def test_empty_script_raises_on_first_call() -> None:
    with pytest.raises(FakeScriptExhaustedError):
        await FakeLLMClient([]).complete(make_request())


async def test_callable_script_sees_each_request() -> None:
    def echo(req: LLMRequest) -> LLMResponse:
        return LLMResponse(text=req.messages[-1].content.upper())

    client = FakeLLMClient(echo)
    assert (await client.complete(make_request("hi"))).text == "HI"
    assert (await client.complete(make_request("yo"))).text == "YO"
    assert len(client.requests) == 2


async def test_default_usage_is_filled_heuristically() -> None:
    client = FakeLLMClient([LLMResponse(text="x" * 9)])
    resp = await client.complete(make_request("a" * 10))
    assert resp.usage == Usage(input_tokens=3, output_tokens=3, llm_calls=1)


async def test_system_prompt_counts_towards_input_tokens() -> None:
    client = FakeLLMClient([LLMResponse(text="")])
    resp = await client.complete(make_request("a" * 4, system="s" * 5))
    assert resp.usage.input_tokens == 1 + 2


async def test_tool_calls_count_towards_output_tokens() -> None:
    call = ToolCall(id="c", name="add", args={"a": 1})
    client = FakeLLMClient([LLMResponse(text="", tool_calls=[call])])
    resp = await client.complete(make_request())
    expected = math.ceil((len("add") + len('{"a": 1}')) / 4)
    assert resp.usage.output_tokens == expected
    assert resp.tool_calls == [call]


async def test_explicit_usage_is_kept_with_one_call() -> None:
    scripted = Usage(input_tokens=1000, output_tokens=7, cost_usd=0.5, llm_calls=9)
    client = FakeLLMClient([LLMResponse(text="x", usage=scripted)])
    resp = await client.complete(make_request())
    assert resp.usage == Usage(
        input_tokens=1000, output_tokens=7, cost_usd=0.5, llm_calls=1
    )


async def test_heuristic_usage_leaves_cost_unknown() -> None:
    client = FakeLLMClient([LLMResponse(text="x")])
    assert (await client.complete(make_request())).usage.cost_usd is None


async def test_script_entries_are_not_mutated() -> None:
    scripted = LLMResponse(text="abc", finish_reason="stop", model="m")
    client = FakeLLMClient([scripted, scripted])
    resp = await client.complete(make_request())
    assert scripted.usage == Usage()
    assert resp.text == "abc"
    assert (resp.finish_reason, resp.model) == ("stop", "m")


async def test_same_script_gives_same_usage() -> None:
    script = [LLMResponse(text="hello world")]
    a = await FakeLLMClient(script).complete(make_request())
    b = await FakeLLMClient(script).complete(make_request())
    assert a == b
