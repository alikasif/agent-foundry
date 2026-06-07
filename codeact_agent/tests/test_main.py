"""Tests for the CLI entry point (main.py).

Covers slash command parsing, banner display, session lifecycle,
Rich output verification, and missing API key error handling.
"""

from __future__ import annotations

import io
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from codeact_agent.main import (
    BANNER,
    SLASH_COMMANDS,
    _display_code,
    _display_result,
    _print_banner,
    _print_help,
    _print_history,
    _print_session_summary,
    main,
)


def _make_console() -> tuple[Console, io.StringIO]:
    """Create a Rich Console backed by a StringIO for output capture."""
    buf = io.StringIO()
    # force_terminal=True ensures Rich renders markup tags as text so we
    # can assert on their content. Without it, tags are stripped and
    # assertions on formatted output become unreliable.
    test_console = Console(
        file=buf,
        force_terminal=True,
        width=200,
        legacy_windows=False,
        color_system=None,
    )
    return test_console, buf


def _render_rich(obj: Any) -> str:
    """Render a Rich renderable to plain text, or str() for plain objects."""
    from rich.console import Console

    # Check if obj is a Rich Renderable (has __rich_console__ method)
    if hasattr(obj, "__rich_console__"):
        buf = io.StringIO()
        c = Console(
            file=buf,
            force_terminal=True,
            width=200,
            legacy_windows=False,
            color_system=None,
        )
        c.print(obj)
        return buf.getvalue()
    return str(obj)


def _get_mock_output(mock_console: MagicMock) -> str:
    """Extract all printed text from a mocked Rich console.

    Collects every call to mock_console.print() and renders Rich
    renderables properly so Panel/Syntax objects yield their text.
    """
    parts: list[str] = []
    for call in mock_console.print.call_args_list:
        args, _ = call
        for arg in args:
            parts.append(_render_rich(arg))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Slash command parsing
# ---------------------------------------------------------------------------


class TestSlashCommands:
    """Tests for slash command parsing and dispatch."""

    @pytest.mark.parametrize(
        "cmd",
        ["help", "reset", "history", "exit", "quit"],
    )
    def test_slash_commands_defined(self, cmd: str) -> None:
        """Each expected slash command must exist in SLASH_COMMANDS dict."""
        assert cmd in SLASH_COMMANDS

    def test_help_description_present(self) -> None:
        """The /help command must have a non-empty description."""
        assert SLASH_COMMANDS["help"]
        assert (
            "Show" in SLASH_COMMANDS["help"]
            or "command" in SLASH_COMMANDS["help"].lower()
        )

    def test_reset_description_present(self) -> None:
        """The /reset command must mention clearing or resetting."""
        desc = SLASH_COMMANDS["reset"].lower()
        assert "clear" in desc or "reset" in desc

    def test_history_description_present(self) -> None:
        """The /history command must mention showing info."""
        desc = SLASH_COMMANDS["history"].lower()
        assert "show" in desc or "history" in desc

    def test_exit_and_quit_same_description(self) -> None:
        """Both /exit and /quit should describe exiting the session."""
        assert SLASH_COMMANDS["exit"] == SLASH_COMMANDS["quit"]

    def test_all_commands_have_descriptions(self) -> None:
        """Every entry in SLASH_COMMANDS must have a non-empty description."""
        for cmd, desc in SLASH_COMMANDS.items():
            assert isinstance(cmd, str) and cmd, f"Invalid command key: {cmd!r}"
            assert isinstance(desc, str) and desc.strip(), (
                f"Empty description for /{cmd}"
            )


# ---------------------------------------------------------------------------
# Banner display
# ---------------------------------------------------------------------------


class TestBannerDisplay:
    """Tests for the startup banner."""

    def test_banner_contains_version(self) -> None:
        """The banner must contain the version string."""
        assert "v0.1.0" in BANNER

    def test_banner_contains_codeact(self) -> None:
        """The banner must mention CodeAct Agent."""
        assert "CodeAct" in BANNER

    def test_print_banner_outputs_model_and_provider(self) -> None:
        """_print_banner must render model and provider info."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_banner("test-model", "https://example.com/api")
        output = buf.getvalue()
        assert "test-model" in output
        assert "https://example.com/api" in output

    def test_print_banner_outputs_python_version(self) -> None:
        """_print_banner must include the Python version."""
        import platform

        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_banner("m", "u")
        output = buf.getvalue()
        assert platform.python_version() in output

    def test_print_banner_lists_slash_commands(self) -> None:
        """_print_banner must list available slash commands (except quit)."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_banner("m", "u")
        output = buf.getvalue()
        assert "/help" in output
        assert "/reset" in output
        assert "/history" in output
        assert "/exit" in output


# ---------------------------------------------------------------------------
# Help output
# ---------------------------------------------------------------------------


class TestPrintHelp:
    """Tests for the _print_help function."""

    def test_print_help_lists_all_commands(self) -> None:
        """_print_help must list every command in SLASH_COMMANDS (except quit)."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_help()
        output = buf.getvalue()
        assert "/help" in output
        assert "/reset" in output
        assert "/history" in output
        assert "/exit" in output

    def test_print_help_does_not_list_quit(self) -> None:
        """_print_help should not list /quit separately from /exit."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_help()
        output = buf.getvalue()
        # /quit should not appear as a separate slash command
        assert "/quit" not in output


# ---------------------------------------------------------------------------
# History output
# ---------------------------------------------------------------------------


class TestPrintHistory:
    """Tests for the _print_history function."""

    def test_print_history_shows_turn_count(self) -> None:
        """_print_history must display the turn count."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_history(5, "test-model")
        output = buf.getvalue()
        assert "5" in output

    def test_print_history_shows_model(self) -> None:
        """_print_history must display the model name."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_history(0, "my-model")
        output = buf.getvalue()
        assert "my-model" in output

    def test_print_history_zero_turns(self) -> None:
        """_print_history must handle zero turns gracefully."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_history(0, "model")
        output = buf.getvalue()
        assert "0" in output


# ---------------------------------------------------------------------------
# Session summary
# ---------------------------------------------------------------------------


class TestPrintSessionSummary:
    """Tests for the _print_session_summary function."""

    def test_session_summary_shows_turns_and_model(self) -> None:
        """_print_session_summary must display turns and model."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_session_summary(3, "test-model")
        output = buf.getvalue()
        assert "3" in output
        assert "test-model" in output

    def test_session_summary_contains_summary_title(self) -> None:
        """_print_session_summary must include a Summary panel."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _print_session_summary(1, "m")
        output = buf.getvalue()
        assert "Summary" in output


# ---------------------------------------------------------------------------
# Rich output verification
# ---------------------------------------------------------------------------


class TestDisplayCode:
    """Tests for the _display_code function."""

    def test_display_code_renders_python_syntax(self) -> None:
        """_display_code must produce output containing the source code."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_code("print('hello')")
        output = buf.getvalue()
        assert "print" in output
        assert "hello" in output

    def test_display_code_multiline(self) -> None:
        """_display_code must handle multi-line code."""
        code = "x = 1\ny = 2\nprint(x + y)"
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_code(code)
        output = buf.getvalue()
        assert "x = 1" in output
        assert "print" in output


class TestDisplayResult:
    """Tests for the _display_result function."""

    def test_display_result_with_stdout(self) -> None:
        """_display_result must render stdout in a green panel."""
        from codeact_agent.sandbox import SandboxResult

        result = SandboxResult(stdout="hello world", stderr="", exception=None)
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_result(result)
        output = buf.getvalue()
        assert "hello world" in output
        assert "stdout" in output

    def test_display_result_with_stderr(self) -> None:
        """_display_result must render stderr in a red panel."""
        from codeact_agent.sandbox import SandboxResult

        result = SandboxResult(stdout="", stderr="error msg", exception=None)
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_result(result)
        output = buf.getvalue()
        assert "error msg" in output
        assert "stderr" in output

    def test_display_result_with_exception(self) -> None:
        """_display_result must render exception in a red panel."""
        from codeact_agent.sandbox import SandboxResult

        result = SandboxResult(
            stdout="", stderr="", exception="ZeroDivisionError: division by zero"
        )
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_result(result)
        output = buf.getvalue()
        assert "ZeroDivisionError" in output
        assert "exception" in output

    def test_display_result_non_sandbox_result_noop(self) -> None:
        """_display_result must return early for non-SandboxResult objects."""
        test_console, buf = _make_console()
        with patch("codeact_agent.main.console", test_console):
            _display_result("not a sandbox result")  # type: ignore[arg-type]
        output = buf.getvalue()
        assert output == ""


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI invocation via CliRunner
# ---------------------------------------------------------------------------


class TestCliRunner:
    """Tests for CLI invocation using click.testing.CliRunner."""

    def test_missing_api_key_exits_with_error(self) -> None:
        """When no API key is provided, CLI must exit with code 1."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            result = runner.invoke(main, [], env={"OPENROUTER_API_KEY": ""})
        assert result.exit_code == 1
        assert (
            "OPENROUTER_API_KEY" in result.output or "api key" in result.output.lower()
        )

    def test_missing_api_key_error_message(self) -> None:
        """The missing API key error must mention how to set it."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            result = runner.invoke(main, [], env={"OPENROUTER_API_KEY": ""})
        assert result.exit_code == 1
        output_lower = result.output.lower()
        assert "openrouter_api_key" in output_lower or "api key" in output_lower

    def test_help_flag_shows_usage(self) -> None:
        """--help must display usage information."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "--model" in result.output
        assert "--api-key" in result.output
        assert "--base-url" in result.output

    def test_default_model_option(self) -> None:
        """The --model option must have the expected default."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        # Click wraps long default values across lines, breaking at hyphens.
        # Remove all whitespace to get a single continuous string for matching.
        normalized = re.sub(r"\s+", "", result.output)
        assert "claude-sonnet-4" in normalized

    def test_default_base_url_option(self) -> None:
        """The --base-url option must have the expected default."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "openrouter.ai" in result.output

    def test_cli_accepts_api_key_option(self) -> None:
        """CLI must accept --api-key as an option."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main._repl") as mock_repl:
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key-123"],
                    env={"OPENROUTER_API_KEY": ""},
                )
                assert result.exit_code == 0
                mock_repl.assert_called_once()

    def test_cli_accepts_model_option(self) -> None:
        """CLI must accept --model and pass it to _repl."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main._repl") as mock_repl:
                result = runner.invoke(
                    main,
                    ["--api-key", "key", "--model", "custom/model"],
                    env={"OPENROUTER_API_KEY": ""},
                )
                assert result.exit_code == 0
                mock_repl.assert_called_once()
                call_kwargs = mock_repl.call_args
                assert call_kwargs.kwargs["model"] == "custom/model"

    def test_cli_accepts_base_url_option(self) -> None:
        """CLI must accept --base-url and pass it to _repl."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main._repl") as mock_repl:
                result = runner.invoke(
                    main,
                    ["--api-key", "key", "--base-url", "https://custom.api/v1"],
                    env={"OPENROUTER_API_KEY": ""},
                )
                assert result.exit_code == 0
                mock_repl.assert_called_once()
                call_kwargs = mock_repl.call_args
                assert call_kwargs.kwargs["base_url"] == "https://custom.api/v1"

    def test_env_var_api_key(self) -> None:
        """CLI must pick up OPENROUTER_API_KEY from environment."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main._repl") as mock_repl:
                result = runner.invoke(
                    main,
                    [],
                    env={"OPENROUTER_API_KEY": "env-key-456"},
                )
                assert result.exit_code == 0
                mock_repl.assert_called_once()
                call_kwargs = mock_repl.call_args
                assert call_kwargs.kwargs["api_key"] == "env-key-456"

    def test_session_lifecycle_exit_via_input(self) -> None:
        """Typing 'exit' must end the session gracefully."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "Session ended" in output

    def test_bare_quit_goes_to_agent(self) -> None:
        """Bare 'quit' (without slash) is sent to the agent, not treated as exit.

        The REPL only treats bare 'exit' as a quit command; bare 'quit'
        falls through to the agent path.
        """
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                # 'quit' goes to agent, which will fail without a real client,
                # then the next input triggers the StopIteration.
                mock_console.input.side_effect = ["quit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        # The agent path is taken (not the exit path), so the result
        # is not a clean exit. This confirms bare 'quit' is not a command.
        assert result.exit_code != 0 or "Session ended" not in _get_mock_output(
            mock_console
        )

    def test_session_lifecycle_slash_exit(self) -> None:
        """Typing '/exit' must end the session gracefully."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "Session ended" in output

    def test_session_lifecycle_slash_quit(self) -> None:
        """Typing '/quit' must end the session gracefully."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/quit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "Session ended" in output

    def test_slash_help_in_repl(self) -> None:
        """Typing '/help' in the REPL must display available commands."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/help", "exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "/help" in output
        assert "/reset" in output
        assert "/history" in output

    def test_slash_history_in_repl(self) -> None:
        """Typing '/history' in the REPL must show turn count and model."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/history", "exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "Turns" in output
        assert "Model" in output

    def test_slash_reset_in_repl(self) -> None:
        """Typing '/reset' in the REPL must show reset confirmation."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/reset", "exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "reset" in output.lower()

    def test_unknown_slash_command(self) -> None:
        """Typing an unknown slash command must show an error."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["/unknown", "exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "Unknown command" in output

    def test_banner_displayed_on_startup(self) -> None:
        """The startup banner must be displayed when the REPL starts."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            with patch("codeact_agent.main.console") as mock_console:
                mock_console.input.side_effect = ["exit"]
                result = runner.invoke(
                    main,
                    ["--api-key", "test-key"],
                    env={"OPENROUTER_API_KEY": ""},
                )
        assert result.exit_code == 0
        output = _get_mock_output(mock_console)
        assert "CodeAct" in output
        assert "v0.1.0" in output

    def test_empty_input_skipped(self) -> None:
        """Empty input lines must be skipped without error."""
        runner = CliRunner()
        with patch("codeact_agent.main.load_dotenv", return_value=None):
            result = runner.invoke(
                main,
                ["--api-key", "test-key"],
                env={"OPENROUTER_API_KEY": ""},
                input="\nexit\n",
            )
        assert result.exit_code == 0
