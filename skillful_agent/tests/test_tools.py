"""Tests verifying the reduced tool list in skillful_agent.tools and agent."""

from __future__ import annotations

import importlib
import inspect

import skillful_agent.tools as tools_module


def test_four_tools_exported() -> None:
    """tools module must expose exactly the four agent-native callables."""
    expected = {
        "get_current_date",
        "execute_bash_command",
        "run_powershell",
        "activate_skill",
    }
    actual = {
        name
        for name in dir(tools_module)
        if callable(getattr(tools_module, name))
        and not name.startswith("_")
        and inspect.isfunction(getattr(tools_module, name))
        # exclude imported functions that are not defined in this module
        and getattr(tools_module, name).__module__ == tools_module.__name__
    }
    assert actual == expected, f"Unexpected tool set: {actual}"


def test_save_reminder_absent() -> None:
    """save_reminder must not be an attribute of the tools module."""
    assert not hasattr(tools_module, "save_reminder"), (
        "save_reminder should have been removed from tools.py"
    )


def test_list_reminders_absent() -> None:
    """list_reminders must not be an attribute of the tools module."""
    assert not hasattr(tools_module, "list_reminders"), (
        "list_reminders should have been removed from tools.py"
    )


def test_reminders_path_constant_absent() -> None:
    """_REMINDERS_PATH constant must not exist in tools module."""
    assert not hasattr(tools_module, "_REMINDERS_PATH"), (
        "_REMINDERS_PATH should have been removed from tools.py"
    )


def test_json_import_absent() -> None:
    """import json must not appear in tools.py source (it was only used by removed fns)."""
    source = inspect.getsource(tools_module)
    assert "import json" not in source, (
        "tools.py still contains 'import json' which should have been removed"
    )


def test_agent_registers_exactly_four_tools() -> None:
    """_build_agent must register exactly four tools in the ADK agent."""
    import os

    os.environ.setdefault("SKILLFUL_MODEL", "test")
    os.environ.setdefault("OPENROUTER_API_KEY", "test")

    from skillful_agent.agent import SkillfulAgent

    agent_instance = SkillfulAgent()
    # ADK LlmAgent exposes registered tools via .tools attribute
    tool_list = agent_instance._agent.tools
    assert len(tool_list) == 4, (  # noqa: PLR2004
        f"Expected exactly 4 tools, got {len(tool_list)}: {tool_list}"
    )


def test_reimport_tools_module() -> None:
    """Reimporting the module must not introduce new attributes."""
    reloaded = importlib.reload(tools_module)
    assert not hasattr(reloaded, "save_reminder")
    assert not hasattr(reloaded, "list_reminders")
    assert not hasattr(reloaded, "_REMINDERS_PATH")
