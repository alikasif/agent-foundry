"""CodeAct strategy: fenced code blocks as actions (designs.md 7.2, FR-004).

Each ``python``/``bash`` block becomes a ``run_code`` tool call, so code runs
through the same validate, permission, hook and trace pipeline as any tool.
Native tool calls for the other registry tools are passed through (U11). A
response with no block and no call is the final answer.

Message shape: block calls are synthesized by the harness, so they never appear
in ``assistant.tool_calls`` (a provider would reject a function response for a
call the model did not make). Their results go back as one ``user`` message of
``<execution>`` observations, as in the sibling loop; native calls are answered
with ``tool`` messages, which come first so they directly follow the call.
"""

import json
from typing import get_args

from harness_builder.core.types import (
    ErrorObservation,
    Message,
    ModelConfig,
    Observation,
    StepDecision,
    ToolCall,
    ToolOutcome,
)
from harness_builder.executor.models import Language
from harness_builder.llm.types import LLMRequest, LLMResponse, ToolSchema
from harness_builder.strategies.codeact._compat import (
    DEFAULT_OUTPUT_MAX_CHARS,
    extract_code_blocks,
    truncate_output,
)
from harness_builder.strategies.codeact.prompt import CODEACT_DOCKER_PROMPT
from harness_builder.strategies.react import ReActStrategy

RUN_CODE = "run_code"
SUPPORTED_LANGUAGES: tuple[str, ...] = get_args(Language)
BLOCK_ID_PREFIX = "codeact_block_"
UNTAGGED_LANGUAGE = "text"


class CodeActStrategy:
    """Code-as-action loop over the ``run_code`` tool plus native tool calls."""

    name: str = "codeact"
    required_tools: frozenset[str] = frozenset({RUN_CODE})

    def __init__(self, output_max_chars: int = DEFAULT_OUTPUT_MAX_CHARS) -> None:
        """``output_max_chars`` caps stdout and stderr per observation (head+tail).

        It is separate from the executor's byte-level output cap (FR-042).
        """
        if output_max_chars <= 0:
            raise ValueError(f"output_max_chars must be > 0, got {output_max_chars}")
        self.output_max_chars = output_max_chars
        self._react = ReActStrategy()

    def system_prompt(self, base: str, tools: list[ToolSchema]) -> str:
        """Return ``base``, the CodeAct prompt and the list of native tools."""
        native = [t for t in tools if t.name != RUN_CODE]
        parts = [base.strip(), CODEACT_DOCKER_PROMPT.strip()]
        if native:
            listing = "\n".join(f"- `{t.name}`: {t.description}" for t in native)
            parts.append(f"## Native Tools\n\n{listing}")
        return "\n\n".join(p for p in parts if p)

    def build_request(
        self, messages: list[Message], tools: list[ToolSchema], model: ModelConfig
    ) -> LLMRequest:
        """Build the request like ReAct, without ``run_code`` as a declaration.

        Code reaches ``run_code`` only through fenced blocks; the other tools
        are sent as native function declarations.
        """
        native = [t for t in tools if t.name != RUN_CODE]
        return self._react.build_request(messages, native, model)

    def parse(self, response: LLMResponse) -> StepDecision:
        """Turn code blocks into ``run_code`` calls and pass native calls through.

        Untagged blocks are prose (for example sample output) and are ignored.
        Any block in an unsupported language makes the whole response a
        ``format`` error and nothing runs. Block calls come first, then native
        calls, each in response order.
        """
        text = response.text
        blocks = [
            (lang, code)
            for lang, code in extract_code_blocks(text)
            if lang != UNTAGGED_LANGUAGE
        ]
        unsupported = sorted({lang for lang, _ in blocks} - set(SUPPORTED_LANGUAGES))
        if unsupported:
            return _format_error(
                text,
                f"unsupported code block language(s) {', '.join(unsupported)}; "
                f"supported: {', '.join(SUPPORTED_LANGUAGES)}",
            )

        native: list[ToolCall] = []
        for index, call in enumerate(response.tool_calls):
            if not call.name.strip():
                return _format_error(text, f"tool call {index} has no name")
            native.append(
                call if call.id else call.model_copy(update={"id": f"call_{index}"})
            )
        if not blocks and not native:
            if not text.strip():
                return _format_error(text, "the response had no text and no code")
            return StepDecision(
                assistant=Message(role="assistant", content=text), final=text
            )

        calls = [
            ToolCall(
                id=f"{BLOCK_ID_PREFIX}{index}",
                name=RUN_CODE,
                args={"language": lang, "code": code},
            )
            for index, (lang, code) in enumerate(blocks)
        ] + native
        ids = [call.id for call in calls]
        if len(set(ids)) != len(ids):
            return _format_error(text, f"tool call ids are not unique: {ids}")
        return StepDecision(
            assistant=Message(role="assistant", content=text, tool_calls=native),
            calls=calls,
        )

    def render_observations(self, results: list[ToolOutcome]) -> list[Message]:
        """Answer native calls with ``tool`` messages, then blocks in one ``user``.

        ``run_code`` results (from a block or a native call) are rendered as
        ``<execution>`` observations with stdout and stderr truncated to
        ``output_max_chars`` each.
        """
        messages = [
            self._tool_message(outcome)
            for outcome in results
            if not _is_block(outcome.call)
        ]
        executions = [
            self._execution(outcome.observation)
            for outcome in results
            if _is_block(outcome.call)
        ]
        if executions:
            messages.append(Message(role="user", content="\n\n".join(executions)))
        return messages

    def _tool_message(self, outcome: ToolOutcome) -> Message:
        """Render a native call's outcome; ``run_code`` keeps the execution shape."""
        if outcome.call.name != RUN_CODE:
            return self._react.render_observations([outcome])[0]
        return Message(
            role="tool",
            content=self._execution(outcome.observation),
            tool_call_id=outcome.call.id,
        )

    def _execution(self, obs: Observation) -> str:
        """Format ``obs`` in the sibling's ``<execution>`` observation shape."""
        result = obs.exec
        if result is None:
            stdout = _content_text(obs)
            stderr = ""
        else:
            stdout = result.stdout
            stderr = result.stderr
            if result.truncated:
                stderr += "\n[output exceeded the sandbox output cap and was cut]"
        parts = [
            "<execution>",
            "<stdout>",
            truncate_output(stdout, self.output_max_chars) if stdout else "",
            "</stdout>",
            "<stderr>",
            truncate_output(stderr, self.output_max_chars) if stderr else "",
            "</stderr>",
            "<exception>",
            _exception(obs),
            "</exception>",
            "</execution>",
        ]
        return "\n".join(parts)


def _is_block(call: ToolCall) -> bool:
    """Return True for a ``run_code`` call synthesized from a code block."""
    return call.name == RUN_CODE and call.id.startswith(BLOCK_ID_PREFIX)


def _content_text(obs: Observation) -> str:
    """Return the observation content as text (dicts as sorted JSON)."""
    if isinstance(obs.content, dict):
        return json.dumps(obs.content, ensure_ascii=False, sort_keys=True)
    return obs.content


def _exception(obs: Observation) -> str:
    """Describe how the execution ended; ``"None"`` when it exited normally."""
    problems: list[str] = []
    if obs.error is not None:
        hint = f" Hint: {obs.error.hint}" if obs.error.hint else ""
        problems.append(f"{obs.error.type}: {obs.error.message}{hint}")
    result = obs.exec
    if result is not None:
        if result.timed_out:
            problems.append("timeout: the code exceeded the time limit and was killed")
        if result.oom_killed:
            problems.append("out_of_memory: the container ran out of memory")
        if result.exit_code is None and not (result.timed_out or result.oom_killed):
            problems.append("the process did not report an exit code")
        if result.exit_code not in (None, 0):
            problems.append(f"exit code {result.exit_code}")
    return "\n".join(problems) or "None"


def _format_error(text: str, problem: str) -> StepDecision:
    """Return a decision carrying a ``format`` error and a call-free assistant."""
    return StepDecision(
        assistant=Message(role="assistant", content=text),
        parse_error=ErrorObservation(
            type="format",
            message=f"Malformed response: {problem}.",
            hint=(
                "Write a fenced ```python or ```bash code block, call a native "
                "tool, or reply with the final answer as plain text."
            ),
        ),
    )
