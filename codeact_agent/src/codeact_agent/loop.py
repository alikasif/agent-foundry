"""AgentLoop -- multi-turn self-debug loop for the CodeAct agent.

Phase A changes
---------------
- ``AgentLoop`` now accepts a ``sandbox: PythonSandbox`` parameter and
  uses it (via the language registry) for all Python execution.  Variables,
  imports, and function definitions persist across blocks and turns.
- A ``LanguageRegistry`` is wired in so all language dispatch goes through
  ``registry.dispatch(lang, code)``.  No per-language ``if/elif`` branching
  in the core loop.
- ``FINAL()`` support: when a Python block calls ``FINAL(answer)``,
  the sandbox sets ``SandboxResult.final_value``.  The loop detects this,
  stops the turn, and returns the final answer immediately.
- ``context`` is set on the sandbox before every user turn via
  ``sandbox.set_context(user_query)`` so agents can always read it.
- ``reset()`` now calls ``sandbox.reset()`` to clear the live namespace.
- The sync/await mismatch from the original implementation is resolved:
  all executors satisfy the ``LanguageExecutor`` protocol (async __call__),
  so every ``await registry.dispatch(...)`` is valid.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from openai.types.responses import ResponseTextDeltaEvent

from codeact_agent.agent import CodeActAgent
from codeact_agent.languages import LanguageRegistry
from codeact_agent.languages.default import build_default_registry
from codeact_agent.sandbox import PythonSandbox, SandboxResult

# ---------------------------------------------------------------------------
# Code block extraction
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(\w+)?\s*\n(.*?)```", re.DOTALL)

# Alias maps: normalise language name variants to canonical names.
_LANG_ALIASES: dict[str, str] = {
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "fish": "bash",
    "py": "python",
    "gawk": "awk",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "sqlite": "sql",
}


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Extract all fenced code blocks from markdown text.

    Args:
        text: Markdown text from an LLM response.

    Returns:
        List of ``(language, code_content)`` tuples.  ``language`` is
        lowercased and normalised via ``_LANG_ALIASES``.  Blocks with no
        language tag default to ``"text"``.
    """
    blocks: list[tuple[str, str]] = []
    for match in _CODE_BLOCK_RE.findall(text):
        raw_lang = (match[0] or "text").lower().strip()
        lang = _LANG_ALIASES.get(raw_lang, raw_lang)
        code = match[1].strip()
        blocks.append((lang, code))
    return blocks


# ---------------------------------------------------------------------------
# Observation formatter
# ---------------------------------------------------------------------------


def _format_observation(result: SandboxResult) -> str:
    """Format a SandboxResult as an XML-tagged user observation message.

    Args:
        result: The sandbox execution result.

    Returns:
        Formatted observation string with XML tags.
    """
    parts = [
        "<execution>",
        "<stdout>",
        result.stdout if result.stdout else "",
        "</stdout>",
        "<stderr>",
        result.stderr if result.stderr else "",
        "</stderr>",
        "<exception>",
        result.exception if result.exception else "None",
        "</exception>",
        "</execution>",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class AgentLoop:
    """Orchestrates the multi-turn conversation: send to LLM, extract code,
    execute via the language registry, observe, repeat.

    Parameters
    ----------
    agent:
        The ``CodeActAgent`` that wraps the LLM.
    sandbox:
        A ``PythonSandbox`` whose namespace persists across turns.
        If ``None``, a fresh sandbox is created.
    registry:
        A ``LanguageRegistry`` for dispatching code blocks.  If ``None``,
        the default registry (Python + bash + awk) is built using *sandbox*.
    max_turns:
        Maximum self-debug iterations per user message (default 20).
    """

    def __init__(
        self,
        agent: CodeActAgent,
        sandbox: PythonSandbox | None = None,
        registry: LanguageRegistry | None = None,
        max_turns: int = 20,
    ) -> None:
        """Initialise the loop.

        Args:
            agent: The CodeActAgent instance.
            sandbox: Optional persistent Python sandbox.  Created if not given.
            registry: Optional language registry.  Built from *sandbox* if not given.
            max_turns: Maximum self-debug turns per user message.
        """
        self._agent = agent
        self._sandbox: PythonSandbox = (
            sandbox if sandbox is not None else PythonSandbox()
        )
        self._registry: LanguageRegistry = (
            registry if registry is not None else build_default_registry(self._sandbox)
        )
        self._max_turns = max_turns

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_turn(
        self,
        history: list[dict[str, str]],
        logger: logging.Logger | None = None,
        on_turn: (
            Callable[[int, str, list[tuple[str, str]], list[SandboxResult]], None]
            | None
        ) = None,
        on_stream_token: Callable[[str], None] | None = None,
        user_query: str | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        """Run one self-debug cycle.

        Sends the conversation history to the agent, extracts any code
        blocks from the response, dispatches them through the language
        registry, and feeds observations back.  Repeats until the agent
        produces a response with no code blocks, until ``FINAL()`` is
        called, or until ``max_turns`` is reached.

        Args:
            history: Current conversation history.
            logger: Optional logger for flow visibility.
            on_turn: Optional callback invoked after each LLM response and
                code execution.  Receives ``(turn_index, response_text,
                code_blocks, execution_results)``.
            on_stream_token: Optional callback for streaming token-by-token
                output.  When provided, LLM responses are streamed.
            user_query: The user's latest query text, used to seed the
                ``context`` variable in the Python namespace.  Extracted
                from the last user message in *history* if not given.

        Returns:
            A tuple of ``(final_response_text, updated_history)``.
            When ``FINAL(answer)`` was called, ``final_response_text``
            will be ``str(answer)`` and the ``SandboxResult.final_value``
            field on the last result will hold the raw value.
        """
        if logger is None:
            logger = logging.getLogger("codeact")

        # Seed context variable in the sandbox namespace.
        query = user_query or _extract_last_user_query(history)
        self._sandbox.set_context(query)

        current_history = list(history)

        for turn_idx in range(self._max_turns):
            logger.debug(
                f"[LLM call] turn={turn_idx + 1}, history_len={len(current_history)}"
            )

            # --- LLM call (streaming or batch) ---
            if on_stream_token is not None:
                stream_result = self._agent.run_streamed(current_history)
                response_parts: list[str] = []
                async for event in stream_result.stream_events():
                    if event.type == "raw_response_event" and isinstance(
                        event.data, ResponseTextDeltaEvent
                    ):
                        delta = event.data.delta
                        response_parts.append(delta)
                        on_stream_token(delta)
                response = "".join(response_parts)
            else:
                response = await self._agent.run(current_history)

            code_blocks = extract_code_blocks(response)

            if not code_blocks:
                logger.debug("[LLM response] no code blocks — final answer returned.")
                if on_turn is not None:
                    on_turn(turn_idx + 1, response, [], [])
                return response, current_history

            logger.debug(f"[LLM response] {len(code_blocks)} code block(s) found.")

            results: list[SandboxResult] = []
            final_answer: str | None = None

            for block_idx, (lang, code) in enumerate(code_blocks, start=1):
                logger.debug(
                    f"[sandbox] executing block {block_idx}/{len(code_blocks)} "
                    f"(language: {lang}) ..."
                )

                result = await self._registry.dispatch(lang, code)
                results.append(result)

                # Check for FINAL() signal.
                if result.final_value is not None:
                    final_answer = str(result.final_value)
                    logger.debug(f"[sandbox] FINAL() called — answer: {final_answer!r}")
                    if on_turn is not None:
                        on_turn(
                            turn_idx + 1, response, code_blocks[:block_idx], results
                        )
                    return final_answer, current_history

                if result.exception:
                    last_line = (
                        result.exception.splitlines()[-1]
                        if result.exception
                        else "None"
                    )
                    logger.debug(
                        f"[sandbox] exception in block {block_idx}: {last_line}"
                    )
                elif result.stdout:
                    preview = result.stdout[:80].replace("\n", "↵")
                    logger.debug(f"[sandbox] stdout preview: {preview}...")

                observation = _format_observation(result)
                current_history = list(current_history)
                current_history.append({"role": "user", "content": observation})

            if on_turn is not None:
                on_turn(turn_idx + 1, response, code_blocks, results)

        logger.warning(
            f"[loop] max_turns={self._max_turns} reached — returning last response."
        )
        return response, current_history

    def reset(self) -> None:
        """Reset the loop: clears the persistent Python namespace."""
        self._sandbox.reset()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_last_user_query(history: list[dict[str, Any]]) -> str:
    """Return the content of the last user-role message in *history*.

    Args:
        history: Conversation history list.

    Returns:
        The content string of the last user message, or ``""`` if none.
    """
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""
