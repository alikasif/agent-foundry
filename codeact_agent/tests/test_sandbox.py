"""Tests for the PythonSandbox execution engine."""

import math

import pytest

from codeact_agent.sandbox import PythonSandbox, SandboxResult


@pytest.fixture
def sandbox() -> PythonSandbox:
    """Return a fresh PythonSandbox instance for each test."""
    return PythonSandbox()


@pytest.mark.anyio
async def test_basic_code_execution(sandbox: PythonSandbox) -> None:
    """Executing 'x = 42' stores the value in the namespace."""
    result = await sandbox.execute("x = 42")
    assert result.exception is None
    assert result.stdout == ""
    assert sandbox.namespace["x"] == 42


@pytest.mark.anyio
async def test_stdout_capture(sandbox: PythonSandbox) -> None:
    """print('hello') is captured in stdout."""
    result = await sandbox.execute("print('hello')")
    assert result.exception is None
    assert "hello" in result.stdout


@pytest.mark.anyio
async def test_stderr_capture(sandbox: PythonSandbox) -> None:
    """sys.stderr.write('err') is captured in stderr."""
    result = await sandbox.execute("import sys\nsys.stderr.write('err')")
    assert result.exception is None
    assert "err" in result.stderr


@pytest.mark.anyio
async def test_exception_capture(sandbox: PythonSandbox) -> None:
    """Executing '1/0' produces a ZeroDivisionError traceback."""
    result = await sandbox.execute("1/0")
    assert result.exception is not None
    assert "ZeroDivisionError" in result.exception


@pytest.mark.anyio
async def test_namespace_persistence(sandbox: PythonSandbox) -> None:
    """Variables defined in one execute() call persist to the next."""
    await sandbox.execute("x = 42")
    result = await sandbox.execute("print(x)")
    assert result.exception is None
    assert "42" in result.stdout


@pytest.mark.anyio
async def test_reset_clears_namespace(sandbox: PythonSandbox) -> None:
    """reset() clears user variables; built-ins FINAL/context are re-injected.

    Phase A: reset() now re-injects FINAL and context into the namespace,
    so the namespace is not empty -- but user-defined names are gone.
    """
    await sandbox.execute("x = 42")
    sandbox.reset()
    assert "x" not in sandbox.namespace
    assert "FINAL" in sandbox.namespace
    assert "context" in sandbox.namespace


@pytest.mark.anyio
async def test_import_persistence(sandbox: PythonSandbox) -> None:
    """Imported modules persist across execute() calls."""
    await sandbox.execute("import math")
    result = await sandbox.execute("print(math.pi)")
    assert result.exception is None
    assert str(math.pi) in result.stdout


@pytest.mark.anyio
async def test_multiline_code_execution(sandbox: PythonSandbox) -> None:
    """Multi-line code blocks execute correctly."""
    code = "x = 1\ny = 2\nprint(x + y)"
    result = await sandbox.execute(code)
    assert result.exception is None
    assert "3" in result.stdout


@pytest.mark.anyio
async def test_sandbox_result_model() -> None:
    """SandboxResult model serializes and deserializes correctly."""
    result = SandboxResult(
        stdout="out",
        stderr="err",
        exception=None,
        return_value=None,
    )
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.exception is None
    assert result.return_value is None


@pytest.mark.anyio
async def test_exception_does_not_break_sandbox(sandbox: PythonSandbox) -> None:
    """Sandbox remains usable after an exception."""
    await sandbox.execute("1/0")
    result = await sandbox.execute("print('still works')")
    assert result.exception is None
    assert "still works" in result.stdout


@pytest.mark.anyio
async def test_stdout_restored_after_execute(sandbox: PythonSandbox) -> None:
    """sys.stdout is restored to its original value after execute()."""
    import sys

    original_stdout = sys.stdout
    await sandbox.execute("print('captured')")
    assert sys.stdout is original_stdout
