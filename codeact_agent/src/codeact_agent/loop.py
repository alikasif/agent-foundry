"""AgentLoop -- multi-turn self-debug loop for the CodeAct agent."""

from __future__ import annotations

import logging
import re

from codeact_agent.agent import CodeActAgent
from codeact_agent.sandbox import PythonSandbox, SandboxResult

_CODE_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


def extract_code_blocks(text: str) -> list[str]:
    """Extract all Python fenced code blocks from markdown text.

    Args:
        text: Markdown text from LLM response.

    Returns:
        List of Python code block contents (without fences).
    """
    return [match.strip() for match in _CODE_BLOCK_RE.findall(text)]


def _format_observation(result: SandboxResult) -> str:
    """Format a SandboxResult as an XML-tagged user observation message.

    Args:
        result: The sandbox execution result.

    Returns:
        Formatted observation string with XML tags.
    """
    parts = ["<execution>"]
    parts.append("<stdout>")
    parts.append(result.stdout if result.stdout else "")
    parts.append("</stdout>")
    parts.append("<stderr>")
    parts.append(result.stderr if result.stderr else "")
    parts.append("</stderr>")
    parts.append("<exception>")
    parts.append(result.exception if result.exception else "None")
    parts.append("</exception>")
    parts.append("</execution>")
    return "\n".join(parts)


class AgentLoop:
    """Orchestrates the multi-turn conversation: send to LLM, extract code,
    execute, observe, repeat."""

    def __init__(
        self,
        agent: CodeActAgent,
        sandbox: PythonSandbox,
        max_turns: int = 20,
    ) -> None:
        """Initialize the AgentLoop.

        Args:
            agent: The CodeActAgent instance.
            sandbox: The PythonSandbox instance.
            max_turns: Maximum number of self-debug turns per user message.
        """
        self._agent = agent
        self._sandbox = sandbox
        self._max_turns = max_turns

    async def run_turn(
        self,
        history: list[dict[str, str]],
        logger: logging.Logger | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        """Run one self-debug cycle.

        Sends the conversation history to the agent, extracts any Python
        code blocks from the response, executes them in the sandbox, and
        feeds the observations back to the agent. Repeats until the agent
        produces a response with no code blocks or max_turns is reached.

        Args:
            history: Current conversation history.
            logger: Optional logger for flow visibility.

        Returns:
            A tuple of (final_response_text, updated_history).
        """
        if logger is None:
            logger = logging.getLogger("codeact")

        current_history = list(history)
        for turn_idx in range(self._max_turns):
            logger.debug(
                f"[LLM call] turn={turn_idx + 1}, "
                f"history_len={len(current_history)}"
            )
            response = await self._agent.run(current_history)
            code_blocks = extract_code_blocks(response)

            if not code_blocks:
                logger.debug(
                    f"[LLM response] no code blocks — final answer returned."
                )
                return response, current_history

            logger.debug(
                f"[LLM response] {len(code_blocks)} code block(s) found."
            )

            for block_idx, code in enumerate(code_blocks, start=1):
                logger.debug(
                    f"[sandbox] executing block {block_idx}/"
                    f"{len(code_blocks)} ..."
                )
                result = await self._sandbox.execute(code)

                if result.exception:
                    logger.debug(
                        f"[sandbox] exception in block {block_idx}: "
                        f"{result.exception.splitlines()[-1]}"
                    )
                elif result.stdout:
                    preview = result.stdout[:80].replace("\n", "↵")
                    logger.debug(
                        f"[sandbox] stdout preview: {preview}..."
                    )

                observation = _format_observation(result)
                current_history = list(current_history)
                current_history.append(
                    {"role": "user", "content": observation}
                )

        logger.warning(
            f"[loop] max_turns={self._max_turns} reached — returning last response."
        )
        return response, current_history

    def reset(self) -> None:
        """Reset the sandbox namespace."""
        self._sandbox.reset()
