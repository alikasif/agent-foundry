"""PythonSandbox -- executes Python code in a persistent namespace.

Phase A additions
-----------------
- ``FinalSignal``: sentinel exception raised by ``FINAL(answer)`` to end a turn.
- ``FINAL(answer)``: callable injected into the namespace before each execution.
  Calling it raises ``FinalSignal`` which the loop catches and surfaces.
- ``context`` variable: injected into the namespace at the start of each turn
  with the user's latest query text (seed for Phase C).
- ``set_context(text)``: update the ``context`` variable between turns.
"""

from __future__ import annotations

import io
import logging
import sys
import traceback

from pydantic import BaseModel


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

    def __init__(self) -> None:
        """Initialize the sandbox with an empty namespace."""
        self._namespace: dict[str, object] = {}
        self._logger = logging.getLogger("codeact.sandbox")
        # Seed the namespace with FINAL and context immediately.
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

    def set_context(self, text: str) -> None:
        """Update the ``context`` variable in the namespace.

        Should be called by the loop at the start of each user turn so
        the agent can always read ``context`` to see the latest query.

        Args:
            text: The user's latest query string.
        """
        self._namespace["context"] = text

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
        """Clear the namespace and re-inject built-ins."""
        self._namespace = {}
        self._inject_builtins()
        self._logger.debug("[exec] namespace cleared.")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _inject_builtins(self) -> None:
        """Inject FINAL() and context into the namespace.

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
        self._namespace.setdefault("context", "")
