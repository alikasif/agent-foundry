"""SubagentRunner -- spawns child RLM agents from within a Python sandbox.

Phase B: Recursive subagent support
-------------------------------------
The ``SubagentRunner`` is the dependency-injection bridge between the
``PythonSandbox`` and ``RLMAgent``/``AgentLoop``.  It is defined here,
not in ``sandbox.py``, to avoid an import cycle:

    sandbox.py  <-- (no import of) -- loop.py / agent.py
    subagents.py imports both and injects itself into the sandbox.

Design decisions
----------------
- **Thread-based async dispatch**: ``exec()`` runs synchronously on the
  main event loop thread.  Calling ``asyncio.run()`` from within a running
  loop raises ``RuntimeError``.  Each ``llm_query`` call spawns a *worker
  thread* via ``concurrent.futures.ThreadPoolExecutor`` which runs its own
  ``asyncio.run()`` in isolation.  The calling (exec) thread blocks on
  ``future.result(timeout=...)``.
- **stdout capture**: child sandboxes use the same in-process stdout
  redirect as the parent.  Running children in separate threads means they
  share ``sys.stdout`` if run concurrently.  Children return answers via
  ``FINAL()``/their return value, never via stdout; the parent reads the
  *return value* only.  A lock prevents concurrent stdout reassignment so
  parallel children don't corrupt each other's capture.  This is documented
  as a known limitation in ``implementation-notes.md``.
- **set_default_openai_client**: ``RLMAgent.__init__`` calls
  ``set_default_openai_client`` which is a module-level global.  All
  children use identical config, so re-setting it is harmless in practice.
  The limitation is documented in ``implementation-notes.md``.
- **No streaming for children**: children run via ``AgentLoop.run_turn``
  with no ``on_stream_token`` or ``on_reasoning_token`` callbacks; they are
  silent.  A single dim progress note is emitted to the parent stdout (when a
  ``progress_fn`` is provided).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid circular import at runtime; only used for type annotations.
    from rlm.context import ContextStore

logger = logging.getLogger("rlm.subagents")

# A callable mapping a recursion depth to the model identifier to use at that
# depth.  Depth 0 is the root agent; depth >= 1 are recursive children.
ModelForDepth = Callable[[int], str]


def default_model_for_depth(root_model: str, sub_model: str) -> ModelForDepth:
    """Return a ``ModelForDepth`` using *root_model* at depth 0 and *sub_model*
    at every deeper level.

    This implements the paper's root/sub model split: a stronger (more
    expensive) model orchestrates at the root while cheaper models handle the
    recursive sub-queries.

    Args:
        root_model: Model identifier for the root agent (depth 0).
        sub_model: Model identifier for all recursive children (depth >= 1).

    Returns:
        A callable ``(depth: int) -> str``.
    """

    def _model_for_depth(depth: int) -> str:
        return root_model if depth == 0 else sub_model

    return _model_for_depth


# Maximum permitted recursion depth for subagents.
_DEFAULT_MAX_DEPTH: int = 3

# Default max turns for a child agent (smaller than parent's 20).
_CHILD_MAX_TURNS: int = 8

# Wall-clock timeout for a single child agent run (seconds).
_CHILD_TIMEOUT_SECONDS: float = 300.0


class SubagentDepthError(Exception):
    """Raised when ``llm_query`` exceeds the maximum recursion depth.

    Inherits from ``Exception`` (not ``BaseException``) so agent code can
    catch it with a normal ``except`` clause.
    """


class SubagentRunner:
    """Builds and drives child agents on behalf of a parent ``PythonSandbox``.

    Parameters
    ----------
    model_for_depth:
        Callable mapping a recursion depth to the model identifier used at
        that depth.  Depth 0 is the root; depth >= 1 are children.  The same
        callable is reused (propagated) at every level so grandchildren route
        consistently.
    api_key:
        API key forwarded to child agents.
    base_url:
        Provider base URL forwarded to child agents.
    depth:
        The current recursion depth.  Root is 0; each child increments.
    max_depth:
        Maximum allowed depth.  Requests at ``max_depth`` raise
        ``SubagentDepthError``.
    child_max_turns:
        ``max_turns`` forwarded to each child ``AgentLoop``.
    progress_fn:
        Optional callable ``(depth: int, prompt_preview: str) -> None``
        called on the *parent* thread just before launching a child.
        Use this to print a dim progress note to the UI.
    """

    def __init__(
        self,
        model_for_depth: ModelForDepth,
        api_key: str,
        base_url: str,
        depth: int = 0,
        max_depth: int = _DEFAULT_MAX_DEPTH,
        child_max_turns: int = _CHILD_MAX_TURNS,
        progress_fn: Callable[[int, str], None] | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            model_for_depth: Callable ``(depth) -> model identifier``.
            api_key: API key for the provider.
            base_url: Base URL for the OpenAI-compatible API.
            depth: Current recursion depth (0 = root).
            max_depth: Hard ceiling; raises at this depth.
            child_max_turns: Max self-debug iterations per child turn.
            progress_fn: Optional UI progress callback.
        """
        self._model_for_depth = model_for_depth
        self._api_key = api_key
        self._base_url = base_url
        self._depth = depth
        self._max_depth = max_depth
        self._child_max_turns = child_max_turns
        self._progress_fn = progress_fn

    # ------------------------------------------------------------------
    # Public API — called by llm_query / llm_query_many in the sandbox
    # ------------------------------------------------------------------

    def run_child(self, prompt: str, context: ContextStore | None = None) -> object:
        """Run a child RLM synchronously and return its ``FINAL()`` value.

        This method is called from within ``exec()`` on the main event-loop
        thread.  It dispatches async work to a worker thread so that the
        child's ``asyncio.run()`` is not called from inside a running loop.
        The child's ``context`` variable is set to a clone of *context*
        (context-by-reference); *prompt* is passed unchanged as the child's
        task.

        Args:
            prompt: The task prompt for the child agent.
            context: Optional ContextStore handed to the child by reference.

        Returns:
            The child agent's ``FINAL()`` value (any Python object).

        Raises:
            SubagentDepthError: If ``max_depth`` would be exceeded.
        """
        if self._depth >= self._max_depth:
            raise SubagentDepthError(
                f"llm_query() recursion depth {self._depth} has reached the "
                f"maximum of {self._max_depth}.  Increase max_depth or "
                f"restructure to avoid deeper nesting."
            )

        if self._progress_fn is not None:
            preview = prompt[:60].replace("\n", " ")
            self._progress_fn(self._depth + 1, preview)

        logger.debug(
            f"[subagent] depth={self._depth} -> spawning child "
            f"(depth={self._depth + 1}): {prompt[:60]!r}"
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._run_child_in_thread, prompt, context)
            result = future.result(timeout=_CHILD_TIMEOUT_SECONDS)

        logger.debug(
            f"[subagent] child (depth={self._depth + 1}) returned: {str(result)[:80]!r}"
        )
        return result

    def run_children_parallel(
        self,
        prompts: list[str],
        contexts: list[ContextStore | None] | None = None,
    ) -> list[object]:
        """Run multiple child RLMs in parallel and return results in order.

        Each child runs in its own worker thread (and thus its own event
        loop).  Results are returned in the same order as *prompts*.  Each
        child's ``context`` is a clone of the aligned entry in *contexts*.

        Thread-safety caveat: child sandboxes swap ``sys.stdout`` during
        execution.  Concurrent swaps can interleave; children must NOT rely
        on stdout capture for correctness (they return via ``FINAL()``).

        Args:
            prompts: List of task prompts.
            contexts: Optional per-prompt ContextStore objects (same length as
                *prompts*, or ``None`` to use no context for any prompt).

        Returns:
            List of child ``FINAL()`` values, in the same order as *prompts*.

        Raises:
            SubagentDepthError: If ``max_depth`` would be exceeded.
        """
        if self._depth >= self._max_depth:
            raise SubagentDepthError(
                f"llm_query_many() recursion depth {self._depth} has reached "
                f"the maximum of {self._max_depth}."
            )

        if contexts is None:
            ctx_list: list[ContextStore | None] = [None] * len(prompts)
        else:
            ctx_list = list(contexts)

        pairs = list(zip(prompts, ctx_list, strict=True))

        if self._progress_fn is not None:
            for p, _c in pairs:
                preview = p[:60].replace("\n", " ")
                self._progress_fn(self._depth + 1, preview)

        logger.debug(
            f"[subagent] depth={self._depth} -> spawning {len(prompts)} "
            f"parallel children (depth={self._depth + 1})"
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            futures = [pool.submit(self._run_child_in_thread, p, c) for p, c in pairs]
            results = [f.result(timeout=_CHILD_TIMEOUT_SECONDS) for f in futures]

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_child_in_thread(self, prompt: str, context: ContextStore | None) -> object:
        """Run a child agent loop in a worker thread.

        This function is executed in a thread (no running event loop),
        so ``asyncio.run()`` is valid here.

        Args:
            prompt: The child agent's task prompt.
            context: Optional ContextStore handed to the child by reference.

        Returns:
            The child's ``FINAL()`` value (any object).
        """
        return asyncio.run(self._run_child_async(prompt, context))

    async def _run_child_async(
        self, prompt: str, context: ContextStore | None
    ) -> object:
        """Async body of the child agent run.

        Args:
            prompt: The child agent's task prompt.
            context: Optional ContextStore handed to the child by reference.

        Returns:
            The child's ``FINAL()`` value, or its display text if ``FINAL()``
            was never called.
        """
        # Import here to avoid module-level circular imports.
        # subagents.py -> agent.py/loop.py is fine at call time.
        from rlm.agent import RLMAgent
        from rlm.context import ContextStore
        from rlm.languages.default import build_default_registry
        from rlm.loop import AgentLoop
        from rlm.sandbox import PythonSandbox

        # Resolve the child's model from its depth (root=0, children>=1).
        child_depth = self._depth + 1
        child_model = self._model_for_depth(child_depth)

        # Build a fresh child runtime.
        child_agent = RLMAgent(
            model=child_model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

        # Child runner propagates depth + 1 so grandchildren are capped, and
        # reuses the SAME model_for_depth callable so routing stays consistent.
        child_runner = SubagentRunner(
            model_for_depth=self._model_for_depth,
            api_key=self._api_key,
            base_url=self._base_url,
            depth=child_depth,
            max_depth=self._max_depth,
            child_max_turns=self._child_max_turns,
            progress_fn=self._progress_fn,
        )

        child_sandbox = PythonSandbox(child_runner=child_runner)
        # Context-by-reference: always clone the given store (or an empty one)
        # into a fresh child-owned file, so the child's `context` never shares
        # a backing file with the parent or any ancestor.
        source_store = context if context is not None else ContextStore.from_string("")
        child_sandbox.set_context_store(source_store.clone())
        child_registry = build_default_registry(child_sandbox)
        child_loop = AgentLoop(
            agent=child_agent,
            sandbox=child_sandbox,
            registry=child_registry,
            max_turns=self._child_max_turns,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": prompt}]

        # Run silent (no stream callbacks) -- children must not garble parent UI.
        display_text, final_value, _ = await child_loop.run_turn(
            history=history,
            user_query=prompt,
        )
        # Return the raw FINAL() object by reference; fall back to display text
        # if the child never called FINAL().
        return final_value if final_value is not None else display_text
