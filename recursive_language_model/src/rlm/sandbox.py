"""PythonSandbox -- executes Python code in a persistent namespace.

Phase A additions
-----------------
- ``FinalSignal``: sentinel exception raised by ``FINAL(answer)`` to end a turn.
- ``FINAL(answer)``: callable injected into the namespace before each execution.
  Calling it raises ``FinalSignal`` which the loop catches and surfaces.
- ``context`` variable: injected into the namespace at the start of each turn
  with the user's latest query text (seed for Phase C).
- ``set_context(text)``: update the ``context`` variable between turns.

Phase B additions
-----------------
- ``child_runner`` parameter: optional ``SubagentRunner`` injected at
  construction time.  When present, the ``llm_query`` and ``llm_query_many``
  builtins are injected into the namespace so agent code can spawn child agents.
  When absent (default), calling those builtins raises a clear ``RuntimeError``
  explaining that no runner was configured.
- ``_inject_builtins()`` extended to inject ``llm_query`` and
  ``llm_query_many`` alongside ``FINAL`` and ``context``.

Phase C additions
-----------------
- ``context`` variable is now a ``ContextStore`` object (context.py) instead
  of a plain string.  For short strings (< 10 000 chars) ``str(context)``
  returns the full text (backward compat); for large inputs it returns a
  summary repr so an accidental ``print(context)`` is safe.
- ``set_context()`` accepts ``str | Path``: strings create an in-memory-backed
  store; paths copy the source file into the workspace.
- A ``workspace`` parameter selects the workspace directory passed through to
  ``ContextStore``.
"""

from __future__ import annotations

import io
import logging
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from rlm.context import ContextStore

if TYPE_CHECKING:
    # Only imported for type annotations; avoid runtime circular import.
    from rlm.subagents import SubagentRunner


class SandboxResult(BaseModel):
    """Result of executing Python code in the sandbox."""

    stdout: str
    stderr: str
    exception: str | None = None
    return_value: object | None = None
    # Set by the loop when FINAL() was called; carries the final answer value.
    final_value: object | None = None


class FinalSignal(BaseException):
    """Raised by FINAL(answer) to terminate the current turn immediately.

    Inherits from BaseException (not Exception) so it is not accidentally
    caught by bare ``except Exception`` clauses inside agent code.
    """

    def __init__(self, value: object) -> None:
        """Initialise with the answer value to return.

        Args:
            value: The answer -- a string, or any Python object.
        """
        super().__init__(value)
        self.value = value


class PythonSandbox:
    """Executes Python code strings in a persistent namespace.

    The namespace persists across ``execute()`` calls within the same
    session.  Use ``reset()`` to start fresh.

    Two special names are always present in the namespace:

    ``FINAL(answer)``
        Ends the current turn immediately and returns ``answer`` as the
        turn's final value.  ``answer`` can be a string or any Python
        object.  The loop catches the resulting ``FinalSignal`` and
        surfaces the value in the CLI.

    ``context``
        The user's latest query text.  Updated by the loop before each
        turn via ``set_context()``.  Phase C will extend this to hold
        large inputs by reference.
    """

    def __init__(
        self,
        child_runner: SubagentRunner | None = None,
        workspace: Path | None = None,
    ) -> None:
        """Initialize the sandbox with an empty namespace.

        Args:
            child_runner: Optional ``SubagentRunner`` used by ``llm_query``
                and ``llm_query_many`` builtins.  When ``None``, those
                builtins raise a clear ``RuntimeError`` when called.
            workspace: Optional workspace directory passed to ``ContextStore``.
                When ``None``, a temporary directory is created per store.
        """
        self._namespace: dict[str, object] = {}
        self._logger = logging.getLogger("rlm.sandbox")
        self._child_runner: SubagentRunner | None = child_runner
        self._workspace: Path | None = workspace
        # Phase C: when True, automatic set_context() calls from the loop
        # are no-ops. Set to True by /load; cleared by reset().
        self._context_locked: bool = False
        # Seed the namespace with FINAL, context, and Phase-B builtins.
        self._inject_builtins()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def namespace(self) -> dict[str, object]:
        """Read access to the namespace for inspection.

        Returns:
            The current namespace dictionary.
        """
        return self._namespace

    def set_context(
        self,
        text: str | Path,
        *,
        lock: bool = False,
        force: bool = False,
    ) -> None:
        """Update the ``context`` variable in the namespace.

        Should be called by the loop at the start of each user turn so
        the agent can always read ``context`` to see the latest query.

        Accepts either:
        - A plain string (Phase A / B API): creates an in-memory-backed
          ``ContextStore``.  For short strings ``str(context)`` returns the
          full text (backward compatible); for large strings it returns a
          summary repr.
        - A ``pathlib.Path``: copies the file into the workspace and creates
          a file-backed ``ContextStore``.

        Args:
            text: The user's latest query string, OR a path to a file to
                load as the context corpus.
            lock: When ``True``, set ``_context_locked = True`` so future
                automatic calls from the loop do not overwrite this context.
                Use this after ``/load`` so the file context persists across
                turns.
            force: When ``True``, update the context even if
                ``_context_locked`` is ``True``.  The ``lock`` flag from
                this call also applies.
        """
        if self._context_locked and not force:
            # Context has been explicitly locked (e.g. via /load).
            # The loop's per-turn set_context calls are no-ops.
            return
        if isinstance(text, Path):
            store = ContextStore.from_path(text, workspace=self._workspace)
        else:
            store = ContextStore.from_string(text, workspace=self._workspace)
        self._namespace["context"] = store
        self._context_locked = lock

    def lock_context(self) -> None:
        """Lock the current context so per-turn loop calls do not overwrite it.

        Called by the ``/load`` command after setting a file-backed context.
        """
        self._context_locked = True

    def set_context_store(self, store: ContextStore, *, lock: bool = True) -> None:
        """Bind an existing ``ContextStore`` directly as ``context``.

        Used by ``SubagentRunner`` to inject a child's context-by-reference
        store (a clone of a parent slice).  Unlike :meth:`set_context`, this
        bypasses string/path construction and binds the given object as-is.

        Args:
            store: The ContextStore to expose as the ``context`` variable.
            lock: When ``True`` (default), set ``_context_locked`` so the
                loop's automatic per-turn ``set_context`` calls do not
                overwrite the injected child context.
        """
        self._namespace["context"] = store
        self._context_locked = lock

    async def execute(
        self,
        code: str,
        timeout: float | None = None,  # reserved for future use
    ) -> SandboxResult:
        """Execute Python code and capture stdout, stderr, and exceptions.

        If the code calls ``FINAL(answer)``, a ``FinalSignal`` is raised
        internally and caught here.  The result will have
        ``final_value`` set and ``exception`` will be ``None``.

        Args:
            code: Python source code to execute.
            timeout: Reserved for future subprocess-based execution.

        Returns:
            A SandboxResult with stdout, stderr, exception, return_value,
            and optionally final_value when FINAL() was called.
        """
        self._logger.debug(f"[exec] code ({len(code)} chars):\n{code}")

        # Always re-inject FINAL so it stays fresh even after a reset.
        self._inject_builtins()

        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        exc_str: str | None = None
        final_val: object | None = None
        caught_final = False

        try:
            exec(code, self._namespace)
        except FinalSignal as fs:
            # FINAL() was called -- not an error.
            final_val = fs.value
            caught_final = True
            self._logger.debug(f"[exec] FINAL() called with value: {final_val!r}")
        except Exception:
            exc_str = traceback.format_exc()
            self._logger.debug(f"[exec] exception:\n{exc_str}")
        finally:
            stdout_val = sys.stdout.getvalue()
            stderr_val = sys.stderr.getvalue()
            sys.stdout, sys.stderr = old_stdout, old_stderr

        _ = caught_final  # silence linter; used above

        result = SandboxResult(
            stdout=stdout_val,
            stderr=stderr_val,
            exception=exc_str,
            return_value=None,
            final_value=final_val,
        )

        self._logger.debug(
            f"[exec] result — stdout={len(result.stdout)} chars, "
            f"stderr={len(result.stderr)} chars, "
            f"exception={'yes' if result.exception else 'no'}, "
            f"final={'yes' if result.final_value is not None else 'no'}"
        )
        return result

    def reset(self) -> None:
        """Clear the namespace and re-inject built-ins.

        Also clears ``_context_locked`` so a freshly-reset sandbox behaves
        like a new one (the loop can seed context again).
        """
        self._namespace = {}
        self._context_locked = False
        self._inject_builtins()
        self._logger.debug("[exec] namespace cleared.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _inject_builtins(self) -> None:
        """Inject FINAL(), llm_query(), llm_query_many(), and context.

        Called on init, on each execute(), and on reset() so these names
        are always available regardless of whether the user redefined them.
        """

        def _final(answer: object) -> None:
            raise FinalSignal(answer)

        _final.__name__ = "FINAL"
        _final.__doc__ = (
            "End the current turn and return *answer* as the final value.\n\n"
            "    FINAL(answer)  ->  terminates execution, surfaces answer in CLI.\n\n"
            "    answer can be a string, a variable name (pass the value directly),\n"
            "    or any Python object.  The CLI will display str(answer)."
        )

        self._namespace["FINAL"] = _final
        # Only seed context if not already set (preserve across turns).
        # Phase C: default is an empty ContextStore, not a plain string.
        self._namespace.setdefault("context", ContextStore(workspace=self._workspace))

        # Phase B: inject llm_query and llm_query_many.
        runner = self._child_runner

        def _reject_str_context(value: object) -> None:
            """Raise a helpful TypeError if a raw string is passed as context."""
            if isinstance(value, str):
                raise TypeError(
                    "llm_query context must be a ContextStore (passed by "
                    "reference), not a str.  Slice the current context with "
                    "context.sub_context(start_line=.., end_line=..) or wrap "
                    "text with ContextStore.from_string(text)."
                )

        def _llm_query(prompt: str, context: ContextStore | None = None) -> object:
            """Spawn a child RLM and return its final answer (by reference).

            The child's ``context`` variable becomes a ContextStore over a
            clone of *context* (context-by-reference): the slice text never
            enters this prompt or the parent LLM's window.  The return value
            is the raw object the child passed to ``FINAL()`` -- it may be any
            Python object (dict/list/dataclass/str), bound directly in your
            namespace without being forced through ``str()``.

            Args:
                prompt: Task description for the child agent.
                context: Optional ContextStore (e.g. a slice from
                    ``context.sub_context(...)``) to hand the child.

            Returns:
                The child agent's ``FINAL()`` value (any object).

            Raises:
                TypeError: If *context* is a raw ``str``.
                SubagentDepthError: If max recursion depth is exceeded.
                RuntimeError: If no child runner is configured.
            """
            _reject_str_context(context)
            if runner is None:
                raise RuntimeError(
                    "llm_query() requires a SubagentRunner injected into the "
                    "PythonSandbox at construction time.  Use the AgentLoop "
                    "or wire a SubagentRunner via PythonSandbox(child_runner=...)."
                )
            return runner.run_child(prompt, context)

        def _llm_query_many(
            prompts: list[str],
            contexts: list[ContextStore | None] | None = None,
        ) -> list[object]:
            """Run multiple child RLMs in parallel, return results in order.

            Each child's ``context`` is a clone of the aligned entry in
            *contexts* (context-by-reference).  Results are returned as a list
            of the children's ``FINAL()`` objects, aligned with *prompts*, and
            never enter the parent LLM's history unless you print() them.

            Args:
                prompts: List of task prompts (one child per prompt).
                contexts: Optional per-prompt ContextStore objects.

            Returns:
                List of child ``FINAL()`` values (any objects) in prompt order.

            Raises:
                TypeError: If any entry in *contexts* is a raw ``str``.
                SubagentDepthError: If max recursion depth is exceeded.
                RuntimeError: If no child runner is configured.
            """
            if contexts is not None:
                for c in contexts:
                    _reject_str_context(c)
            if runner is None:
                raise RuntimeError(
                    "llm_query_many() requires a SubagentRunner injected into "
                    "the PythonSandbox at construction time."
                )
            return runner.run_children_parallel(prompts, contexts)

        self._namespace["llm_query"] = _llm_query
        self._namespace["llm_query_many"] = _llm_query_many
