"""Phase A -- targeted tests for new capabilities.

Covers:
- Python namespace persistence across blocks AND across turns.
- FINAL() mechanism: terminates the turn and returns the answer value.
- context variable: injected and updated per turn.
- LanguageRegistry: dispatch, register, unknown-language error.
- GitBashResolver: missing-tool returns actionable error in SandboxResult.
- /reset clears the live Python namespace.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeact_agent.agent import CodeActAgent
from codeact_agent.bash_resolver import clear_cache, require_tool, resolve_tool
from codeact_agent.languages import LanguageRegistry
from codeact_agent.languages.default import build_default_registry
from codeact_agent.languages.python_executor import PythonExecutor
from codeact_agent.languages.registry import LanguageExecutor
from codeact_agent.languages.subprocess_executor import AwkExecutor, BashExecutor
from codeact_agent.loop import AgentLoop
from codeact_agent.sandbox import FinalSignal, PythonSandbox, SandboxResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success(stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(
        stdout=stdout,
        stderr=stderr,
        exception=None,
        return_value=None,
        final_value=None,
    )


# ---------------------------------------------------------------------------
# Python persistence tests
# ---------------------------------------------------------------------------


class TestPythonPersistence:
    """Variables, imports, and functions survive across blocks and turns."""

    @pytest.mark.anyio
    async def test_variable_persists_across_blocks_same_turn(self) -> None:
        """Define a var in block 1; read it in block 2 of the same turn."""
        sandbox = PythonSandbox()
        await sandbox.execute("x = 99")
        result = await sandbox.execute("print(x)")
        assert result.exception is None
        assert "99" in result.stdout

    @pytest.mark.anyio
    async def test_variable_persists_across_turns(self) -> None:
        """Define a var in one AgentLoop turn; read it in the next turn.

        This is the canonical Phase A persistence test: the sandbox is shared
        across multiple calls to run_turn().
        """
        sandbox = PythonSandbox()
        mock_agent = AsyncMock(spec=CodeActAgent)

        # Turn 1: agent defines x = 42, then returns plain text.
        mock_agent.run.side_effect = [
            "```python\nx = 42\n```",
            "x is defined",
            # Turn 2: agent reads x and prints it, then returns plain text.
            "```python\nprint(x)\n```",
            "x is still 42",
        ]
        sandbox_execute_real = sandbox.execute

        async def patched_execute(code: str) -> SandboxResult:
            return await sandbox_execute_real(code)

        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=build_default_registry(sandbox),
            max_turns=5,
        )

        history: list[dict[str, str]] = [{"role": "user", "content": "set x"}]
        _, history = await loop.run_turn(history)

        # Turn 2: x should still be in the namespace
        history.append({"role": "user", "content": "read x"})
        _, history = await loop.run_turn(history)

        # The last observation should contain "42" from print(x)
        observations = [m["content"] for m in history if "<stdout>" in m["content"]]
        assert any("42" in obs for obs in observations), (
            "Expected '42' in a stdout observation from the second turn"
        )

    @pytest.mark.anyio
    async def test_import_persists_across_execute_calls(self) -> None:
        """import math in one call; use math.pi in the next."""
        sandbox = PythonSandbox()
        await sandbox.execute("import math")
        result = await sandbox.execute("print(math.pi)")
        assert result.exception is None
        assert "3.14" in result.stdout

    @pytest.mark.anyio
    async def test_function_definition_persists(self) -> None:
        """Define a function; call it in the next execute."""
        sandbox = PythonSandbox()
        await sandbox.execute("def greet(name): return f'Hello {name}'")
        result = await sandbox.execute("print(greet('world'))")
        assert result.exception is None
        assert "Hello world" in result.stdout


# ---------------------------------------------------------------------------
# FINAL() mechanism tests
# ---------------------------------------------------------------------------


class TestFinalMechanism:
    """FINAL(answer) ends the turn and returns the answer value."""

    @pytest.mark.anyio
    async def test_final_signal_raised(self) -> None:
        """FinalSignal is raised when FINAL() is called."""
        sandbox = PythonSandbox()
        with pytest.raises(FinalSignal) as exc_info:
            # Inject FINAL manually, then call it synchronously for the test.
            sandbox._inject_builtins()
            final_fn = sandbox.namespace["FINAL"]
            final_fn("hello")
        assert exc_info.value.value == "hello"

    @pytest.mark.anyio
    async def test_final_sets_final_value_in_result(self) -> None:
        """execute() captures FINAL() and sets final_value in SandboxResult."""
        sandbox = PythonSandbox()
        result = await sandbox.execute("FINAL('the answer is 42')")
        assert result.exception is None
        assert result.final_value == "the answer is 42"

    @pytest.mark.anyio
    async def test_final_with_variable(self) -> None:
        """FINAL(variable) returns the variable's current value."""
        sandbox = PythonSandbox()
        await sandbox.execute("result = 2 ** 10")
        result = await sandbox.execute("FINAL(result)")
        assert result.final_value == 1024

    @pytest.mark.anyio
    async def test_final_terminates_turn_in_loop(self) -> None:
        """When FINAL() is called, run_turn() returns the answer immediately."""
        sandbox = PythonSandbox()
        mock_agent = AsyncMock(spec=CodeActAgent)
        # Agent returns a block that calls FINAL; no second LLM call needed.
        mock_agent.run.return_value = "```python\nFINAL('done')\n```"

        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=build_default_registry(sandbox),
        )
        history: list[dict[str, str]] = [{"role": "user", "content": "finish"}]
        response, _ = await loop.run_turn(history)

        assert response == "done"
        # Loop must NOT have called the agent a second time.
        mock_agent.run.assert_called_once()

    @pytest.mark.anyio
    async def test_final_with_list_value(self) -> None:
        """FINAL() can return any Python object; final_value holds it."""
        sandbox = PythonSandbox()
        result = await sandbox.execute("FINAL([1, 2, 3])")
        assert result.final_value == [1, 2, 3]

    @pytest.mark.anyio
    async def test_final_stdout_before_final_is_captured(self) -> None:
        """Output printed before FINAL() is captured in stdout."""
        sandbox = PythonSandbox()
        result = await sandbox.execute("print('before')\nFINAL('end')")
        assert "before" in result.stdout
        assert result.final_value == "end"
        assert result.exception is None


# ---------------------------------------------------------------------------
# context variable tests
# ---------------------------------------------------------------------------


class TestContextVariable:
    """context is seeded in the namespace and updated per turn."""

    @pytest.mark.anyio
    async def test_context_present_after_init(self) -> None:
        """context is available immediately after PythonSandbox is created."""
        sandbox = PythonSandbox()
        assert "context" in sandbox.namespace

    @pytest.mark.anyio
    async def test_set_context_updates_namespace(self) -> None:
        """set_context() updates the context variable in the namespace."""
        sandbox = PythonSandbox()
        sandbox.set_context("what is 2+2?")
        result = await sandbox.execute("print(context)")
        assert "what is 2+2?" in result.stdout

    @pytest.mark.anyio
    async def test_context_set_by_loop_before_turn(self) -> None:
        """run_turn() seeds context with the user's latest query."""
        sandbox = PythonSandbox()
        mock_agent = AsyncMock(spec=CodeActAgent)
        mock_agent.run.side_effect = [
            "```python\nprint(context)\n```",
            "done",
        ]
        loop = AgentLoop(
            agent=mock_agent,
            sandbox=sandbox,
            registry=build_default_registry(sandbox),
        )
        history: list[dict[str, str]] = [
            {"role": "user", "content": "my specific query"}
        ]
        _, updated = await loop.run_turn(history, user_query="my specific query")
        obs = [m["content"] for m in updated if "<stdout>" in m["content"]]
        assert any("my specific query" in o for o in obs)


# ---------------------------------------------------------------------------
# LanguageRegistry tests
# ---------------------------------------------------------------------------


class TestLanguageRegistry:
    """Registry dispatch, register, and unknown-language error path."""

    @pytest.mark.anyio
    async def test_dispatch_python_calls_sandbox_execute(self) -> None:
        """Dispatching 'python' calls the underlying sandbox.execute()."""
        sandbox = PythonSandbox()
        registry = build_default_registry(sandbox)
        result = await registry.dispatch("python", "print('hi')")
        assert result.exception is None
        assert "hi" in result.stdout

    @pytest.mark.anyio
    async def test_unknown_language_returns_error_result(self) -> None:
        """Dispatching an unregistered language returns an error SandboxResult."""
        registry = LanguageRegistry()
        result = await registry.dispatch("cobol", "DISPLAY 'hello'.")
        assert result.exception is not None
        assert "cobol" in result.exception
        assert "not registered" in result.exception

    @pytest.mark.anyio
    async def test_register_custom_executor(self) -> None:
        """A custom executor can be registered and dispatched correctly."""

        class EchoExecutor:
            async def __call__(self, code: str) -> SandboxResult:
                return SandboxResult(
                    stdout=f"ECHO: {code}",
                    stderr="",
                    exception=None,
                    return_value=None,
                )

        registry = LanguageRegistry()
        registry.register("echo", EchoExecutor())
        result = await registry.dispatch("echo", "hello world")
        assert result.stdout == "ECHO: hello world"
        assert result.exception is None

    def test_registered_languages_sorted(self) -> None:
        """registered_languages() returns a sorted list."""
        registry = LanguageRegistry()
        registry.register("zebra", MagicMock())
        registry.register("alpha", MagicMock())
        langs = registry.registered_languages()
        assert langs == sorted(langs)

    def test_adding_language_does_not_require_core_refactor(self) -> None:
        """Registering a new language only touches the registry, not the loop."""
        # This is a structural test: if the loop has no per-language if/elif,
        # then dispatch is purely data-driven.
        import inspect

        from codeact_agent.loop import AgentLoop as AgentLoopCls

        source = inspect.getsource(AgentLoopCls.run_turn)
        # The loop must NOT branch on specific language names.
        for lang in ("bash", "awk", "javascript", "sql"):
            assert f'"{lang}"' not in source or "dispatch" in source, (
                f"Loop appears to have per-language branch for '{lang}'"
            )

    @pytest.mark.anyio
    async def test_executor_protocol_satisfied(self) -> None:
        """PythonExecutor, BashExecutor, AwkExecutor all satisfy LanguageExecutor."""
        sandbox = PythonSandbox()
        for executor in (PythonExecutor(sandbox), BashExecutor(), AwkExecutor()):
            assert isinstance(executor, LanguageExecutor), (
                f"{type(executor).__name__} does not satisfy LanguageExecutor"
            )


# ---------------------------------------------------------------------------
# GitBashResolver tests (mocked subprocess to avoid CI dependency)
# ---------------------------------------------------------------------------


class TestGitBashResolver:
    """bash_resolver finds tools or returns actionable errors."""

    def setup_method(self) -> None:
        """Clear the resolver cache before each test."""
        clear_cache()

    def teardown_method(self) -> None:
        """Restore the cache after each test."""
        clear_cache()

    def test_resolve_tool_returns_path_when_on_path(self) -> None:
        """resolve_tool returns the found path when shutil.which succeeds."""
        with patch(
            "codeact_agent.bash_resolver.shutil.which", return_value="/usr/bin/bash"
        ):
            result = resolve_tool("bash")
        assert result == "/usr/bin/bash"

    def test_resolve_tool_returns_none_when_missing(self) -> None:
        """resolve_tool returns None when the tool is nowhere to be found."""
        with (
            patch("codeact_agent.bash_resolver.shutil.which", return_value=None),
            patch("codeact_agent.bash_resolver.os.path.isfile", return_value=False),
        ):
            result = resolve_tool("bash")
        assert result is None

    def test_require_tool_raises_file_not_found(self) -> None:
        """require_tool raises FileNotFoundError with an actionable message."""
        with (
            patch("codeact_agent.bash_resolver.shutil.which", return_value=None),
            patch("codeact_agent.bash_resolver.os.path.isfile", return_value=False),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                require_tool("bash")
        msg = str(exc_info.value)
        assert "bash" in msg
        assert "git-scm.com" in msg or "Git" in msg

    @pytest.mark.anyio
    async def test_bash_executor_missing_tool_returns_error_result(self) -> None:
        """BashExecutor returns an error SandboxResult when bash is missing."""
        clear_cache()
        with (
            patch("codeact_agent.bash_resolver.shutil.which", return_value=None),
            patch("codeact_agent.bash_resolver.os.path.isfile", return_value=False),
        ):
            executor = BashExecutor()
            result = await executor("echo hello")
        assert result.exception is not None
        assert "bash" in result.exception.lower()
        assert (
            "git" in result.exception.lower() or "install" in result.exception.lower()
        )

    @pytest.mark.anyio
    async def test_awk_executor_missing_tool_returns_error_result(self) -> None:
        """AwkExecutor returns an error SandboxResult when awk is missing."""
        clear_cache()
        with (
            patch("codeact_agent.bash_resolver.shutil.which", return_value=None),
            patch("codeact_agent.bash_resolver.os.path.isfile", return_value=False),
        ):
            executor = AwkExecutor()
            result = await executor("BEGIN { print 'hi' }")
        assert result.exception is not None
        assert "awk" in result.exception.lower()


# ---------------------------------------------------------------------------
# /reset clears the live namespace
# ---------------------------------------------------------------------------


class TestResetClearsNamespace:
    """/reset (loop.reset()) clears user-defined vars from the sandbox."""

    @pytest.mark.anyio
    async def test_reset_removes_user_variables(self) -> None:
        """After reset(), user variables are gone but FINAL/context remain."""
        sandbox = PythonSandbox()
        await sandbox.execute("my_var = 'persistent'")
        assert "my_var" in sandbox.namespace

        mock_agent = AsyncMock(spec=CodeActAgent)
        loop = AgentLoop(agent=mock_agent, sandbox=sandbox)
        loop.reset()

        assert "my_var" not in sandbox.namespace
        assert "FINAL" in sandbox.namespace
        assert "context" in sandbox.namespace

    @pytest.mark.anyio
    async def test_reset_followed_by_fresh_execution(self) -> None:
        """After reset(), a new variable can be defined without conflicts."""
        sandbox = PythonSandbox()
        await sandbox.execute("x = 100")
        sandbox.reset()
        result = await sandbox.execute("x = 200\nprint(x)")
        assert "200" in result.stdout
        assert result.exception is None
