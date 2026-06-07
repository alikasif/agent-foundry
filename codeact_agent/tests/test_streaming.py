"""Tests for live streaming of reasoning and answer deltas in AgentLoop.

Verifies that ``run_turn`` forwards ``ResponseReasoningTextDeltaEvent`` deltas
to ``on_reasoning_token`` (display-only), ``ResponseTextDeltaEvent`` deltas to
``on_stream_token``, calls ``on_llm_start`` before any token arrives, and that
the code-extraction path sees only the text deltas (not reasoning).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.responses import (
    ResponseReasoningTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from codeact_agent.agent import CodeActAgent
from codeact_agent.loop import AgentLoop
from codeact_agent.sandbox import PythonSandbox


def _raw_event(data: Any) -> MagicMock:
    """Wrap event data as a raw_response_event-like stream event."""
    event = MagicMock()
    event.type = "raw_response_event"
    event.data = data
    return event


def _reasoning_event(delta: str) -> MagicMock:
    """Build a raw event wrapping a ResponseReasoningTextDeltaEvent."""
    return _raw_event(ResponseReasoningTextDeltaEvent.model_construct(delta=delta))


def _text_event(delta: str) -> MagicMock:
    """Build a raw event wrapping a ResponseTextDeltaEvent."""
    return _raw_event(ResponseTextDeltaEvent.model_construct(delta=delta))


def _agent_updated_event() -> MagicMock:
    """Build a non-raw event that the loop must skip (e.g. agent_updated)."""
    event = MagicMock()
    event.type = "agent_updated_stream_event"
    event.data = MagicMock()
    return event


def _fake_stream(events: list[MagicMock]) -> MagicMock:
    """Return a stream-result mock whose stream_events() async-yields events."""

    async def _gen() -> AsyncIterator[MagicMock]:
        for event in events:
            yield event

    stream_result = MagicMock()
    stream_result.stream_events = MagicMock(return_value=_gen())
    return stream_result


@pytest.fixture
def mock_agent() -> AsyncMock:
    """Async mock satisfying the CodeActAgent interface."""
    return AsyncMock(spec=CodeActAgent)


@pytest.fixture
def real_sandbox() -> PythonSandbox:
    """Real sandbox so a no-code answer returns immediately."""
    return PythonSandbox()


class TestStreamingCallbacks:
    """Tests for reasoning/text/start streaming callbacks."""

    @pytest.mark.anyio
    async def test_reasoning_and_text_routed_separately(
        self, mock_agent: AsyncMock, real_sandbox: PythonSandbox
    ) -> None:
        """Reasoning deltas go to on_reasoning_token, text deltas to on_stream_token."""
        events = [
            _agent_updated_event(),
            _reasoning_event("think "),
            _reasoning_event("more "),
            _text_event("answer"),
        ]
        # run_streamed is called WITHOUT await, so it must be a sync MagicMock.
        mock_agent.run_streamed = MagicMock(return_value=_fake_stream(events))

        loop = AgentLoop(agent=mock_agent, sandbox=real_sandbox, max_turns=3)

        reasoning: list[str] = []
        text: list[str] = []
        order: list[str] = []

        def on_reasoning(delta: str) -> None:
            reasoning.append(delta)
            order.append("reasoning")

        def on_text(delta: str) -> None:
            text.append(delta)
            order.append("text")

        def on_start() -> None:
            order.append("start")

        history = [{"role": "user", "content": "hi"}]
        response, _ = await loop.run_turn(
            history,
            on_stream_token=on_text,
            on_reasoning_token=on_reasoning,
            on_llm_start=on_start,
        )

        assert reasoning == ["think ", "more "]
        assert text == ["answer"]
        # on_llm_start fires before any token of the call.
        assert order[0] == "start"
        assert order.index("start") < order.index("reasoning")
        assert order.index("start") < order.index("text")

    @pytest.mark.anyio
    async def test_code_extraction_sees_only_text(
        self, mock_agent: AsyncMock, real_sandbox: PythonSandbox
    ) -> None:
        """The returned response (code-extraction input) excludes reasoning."""
        events = [
            _reasoning_event("I should write `print` "),
            _text_event("The answer "),
            _text_event("is 42."),
        ]
        mock_agent.run_streamed = MagicMock(return_value=_fake_stream(events))

        loop = AgentLoop(agent=mock_agent, sandbox=real_sandbox, max_turns=3)

        response, _ = await loop.run_turn(
            history=[{"role": "user", "content": "q"}],
            on_stream_token=lambda d: None,
            on_reasoning_token=lambda d: None,
        )

        # Only the text deltas — no reasoning text leaked in.
        assert response == "The answer is 42."
        assert "I should write" not in response

    @pytest.mark.anyio
    async def test_streaming_works_without_optional_callbacks(
        self, mock_agent: AsyncMock, real_sandbox: PythonSandbox
    ) -> None:
        """on_reasoning_token / on_llm_start are optional; text still streams."""
        events = [_reasoning_event("hmm "), _text_event("done")]
        mock_agent.run_streamed = MagicMock(return_value=_fake_stream(events))

        loop = AgentLoop(agent=mock_agent, sandbox=real_sandbox, max_turns=3)

        text: list[str] = []
        response, _ = await loop.run_turn(
            history=[{"role": "user", "content": "q"}],
            on_stream_token=text.append,
        )

        assert text == ["done"]
        assert response == "done"
