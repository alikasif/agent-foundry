"""Tests for strategies/react.py (designs.md 7.1, FR-003)."""

import json

import pytest

from harness_builder.core.types import (
    ErrorObservation,
    Message,
    ModelConfig,
    Observation,
    ToolCall,
    ToolOutcome,
)
from harness_builder.llm.fake import FakeLLMClient
from harness_builder.llm.types import LLMResponse, ToolSchema
from harness_builder.strategies.react import REACT_GUIDANCE, ReActStrategy

MODEL = ModelConfig(name="fake/model")
ECHO = ToolSchema(
    name="echo",
    description="Echo text.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
)


@pytest.fixture
def strategy() -> ReActStrategy:
    """A fresh ReAct strategy."""
    return ReActStrategy()


def outcome(call_id: str, **obs: object) -> ToolOutcome:
    """Build a ``ToolOutcome`` for ``call_id`` with the given observation fields."""
    call = ToolCall(id=call_id, name="echo")
    return ToolOutcome(
        call=call,
        observation=Observation.model_validate({"call_id": call_id, **obs}),
    )


def test_class_attributes(strategy: ReActStrategy) -> None:
    assert strategy.name == "react"
    assert strategy.required_tools == frozenset()


# ------------------------------------------------------------- system_prompt


def test_system_prompt_appends_guidance(strategy: ReActStrategy) -> None:
    assert strategy.system_prompt("Be helpful.\n", [ECHO]) == (
        f"Be helpful.\n\n{REACT_GUIDANCE}"
    )


@pytest.mark.parametrize("base", ["", "   \n"])
def test_system_prompt_blank_base(strategy: ReActStrategy, base: str) -> None:
    assert strategy.system_prompt(base, []) == REACT_GUIDANCE


# ------------------------------------------------------------- build_request


def test_build_request_lifts_leading_system_messages(
    strategy: ReActStrategy,
) -> None:
    messages = [
        Message(role="system", content="one"),
        Message(role="system", content="two"),
        Message(role="user", content="hi"),
        Message(role="system", content="summary"),
        Message(role="assistant", content="hello"),
    ]
    req = strategy.build_request(messages, [ECHO], MODEL)
    assert req.system == "one\n\ntwo"
    assert req.messages == messages[2:]
    assert req.tools == [ECHO]
    assert req.model == MODEL
    assert req.purpose == "step"


def test_build_request_without_system(strategy: ReActStrategy) -> None:
    messages = [Message(role="user", content="hi")]
    req = strategy.build_request(messages, [], MODEL)
    assert req.system is None
    assert req.messages == messages
    assert req.tools == []


def test_build_request_does_not_alias_inputs(strategy: ReActStrategy) -> None:
    messages = [Message(role="user", content="hi")]
    tools = [ECHO]
    req = strategy.build_request(messages, tools, MODEL)
    req.messages.append(Message(role="user", content="more"))
    req.tools.append(ECHO)
    assert len(messages) == 1 and len(tools) == 1


def test_build_request_tool_schemas_pass_through(strategy: ReActStrategy) -> None:
    req = strategy.build_request([Message(role="user", content="x")], [ECHO], MODEL)
    assert req.tools[0].parameters == ECHO.parameters


# --------------------------------------------------------------------- parse


def test_parse_text_is_final(strategy: ReActStrategy) -> None:
    decision = strategy.parse(LLMResponse(text="The answer is 4."))
    assert decision.final == "The answer is 4."
    assert decision.calls == []
    assert decision.parse_error is None
    assert decision.assistant == Message(role="assistant", content="The answer is 4.")


def test_parse_calls(strategy: ReActStrategy) -> None:
    calls = [
        ToolCall(id="a", name="echo", args={"text": "hi"}),
        ToolCall(id="b", name="nope_unknown_tool", args={}),
    ]
    decision = strategy.parse(LLMResponse(text="Let me check.", tool_calls=calls))
    assert decision.calls == calls
    assert decision.final is None
    assert decision.parse_error is None
    assert decision.assistant.content == "Let me check."
    assert decision.assistant.tool_calls == calls


def test_parse_assigns_deterministic_ids(strategy: ReActStrategy) -> None:
    calls = [ToolCall(id="", name="echo"), ToolCall(id="x", name="echo")]
    decision = strategy.parse(LLMResponse(tool_calls=calls))
    assert [c.id for c in decision.calls] == ["call_0", "x"]
    assert decision.assistant.tool_calls == decision.calls


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(LLMResponse(text=""), id="empty"),
        pytest.param(LLMResponse(text="  \n\t"), id="blank-text"),
        pytest.param(
            LLMResponse(text="t", tool_calls=[ToolCall(id="a", name=" ")]),
            id="blank-name",
        ),
        pytest.param(
            LLMResponse(
                tool_calls=[ToolCall(id="a", name="x"), ToolCall(id="a", name="y")]
            ),
            id="duplicate-ids",
        ),
        pytest.param(
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call_1", name="x"),
                    ToolCall(id="", name="y"),
                ]
            ),
            id="generated-id-collides",
        ),
    ],
)
def test_parse_garbage_is_format_error(
    strategy: ReActStrategy, response: LLMResponse
) -> None:
    decision = strategy.parse(response)
    error = decision.parse_error
    assert error is not None
    assert error.type == "format"
    assert error.message.startswith("Malformed response")
    assert error.hint
    assert decision.calls == []
    assert decision.final is None
    assert decision.assistant.role == "assistant"
    assert decision.assistant.tool_calls == []
    assert decision.assistant.content == response.text


# ------------------------------------------------------- render_observations


def test_render_error_shape(strategy: ReActStrategy) -> None:
    err = ErrorObservation(type="validation", message="bad arg")
    [msg] = strategy.render_observations(
        [outcome("c1", ok=False, content="ignored", error=err)]
    )
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1"
    assert json.loads(msg.content) == {
        "error": {"type": "validation", "message": "bad arg", "hint": None}
    }


def test_render_error_with_hint(strategy: ReActStrategy) -> None:
    err = ErrorObservation(type="unknown_tool", message="no tool", hint="use echo")
    [msg] = strategy.render_observations([outcome("c1", ok=False, error=err)])
    assert json.loads(msg.content)["error"]["hint"] == "use echo"


def test_render_dict_content_sorted_and_unicode(strategy: ReActStrategy) -> None:
    [msg] = strategy.render_observations(
        [outcome("c1", ok=True, content={"b": "é", "a": 1})]
    )
    assert msg.content == '{"a": 1, "b": "é"}'


def test_render_string_content_verbatim(strategy: ReActStrategy) -> None:
    [msg] = strategy.render_observations(
        [outcome("c1", ok=True, content="plain\ntext", final="ignored")]
    )
    assert msg.content == "plain\ntext"


def test_render_final_when_content_empty(strategy: ReActStrategy) -> None:
    [msg] = strategy.render_observations(
        [outcome("c1", ok=True, content="", final="the end")]
    )
    assert msg.content == "the end"


def test_render_empty(strategy: ReActStrategy) -> None:
    [msg] = strategy.render_observations([outcome("c1", ok=True)])
    assert msg.content == ""
    assert strategy.render_observations([]) == []


def test_render_preserves_order_and_ids(strategy: ReActStrategy) -> None:
    msgs = strategy.render_observations(
        [outcome(cid, ok=True, content=cid) for cid in ("z", "a", "m")]
    )
    assert [(m.tool_call_id, m.content) for m in msgs] == [
        ("z", "z"),
        ("a", "a"),
        ("m", "m"),
    ]


# ------------------------------------------------------ FakeLLMClient round-trip


@pytest.mark.anyio
async def test_round_trip_with_fake_client(strategy: ReActStrategy) -> None:
    call = ToolCall(id="c1", name="echo", args={"text": "hi"})
    fake = FakeLLMClient([LLMResponse(tool_calls=[call]), LLMResponse(text="said hi")])
    messages = [
        Message(role="system", content=strategy.system_prompt("Base.", [ECHO])),
        Message(role="user", content="say hi"),
    ]

    decision = strategy.parse(
        await fake.complete(strategy.build_request(messages, [ECHO], MODEL))
    )
    assert decision.calls == [call]
    first = fake.requests[0]
    assert first.system is not None and first.system.endswith(REACT_GUIDANCE)
    assert [m.role for m in first.messages] == ["user"]
    assert first.tools == [ECHO]

    messages += [decision.assistant]
    messages += strategy.render_observations(
        [
            ToolOutcome(
                call=call, observation=Observation(call_id="c1", ok=True, content="hi")
            )
        ]
    )
    final = strategy.parse(
        await fake.complete(strategy.build_request(messages, [ECHO], MODEL))
    )
    assert final.final == "said hi"
    second = fake.requests[1]
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    assert second.messages[-1].tool_call_id == "c1"
