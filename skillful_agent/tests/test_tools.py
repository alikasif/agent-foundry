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


# ---------------------------------------------------------------------------
# activate_skill mode tests (T004)
# ---------------------------------------------------------------------------


from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from skillful_agent.skill_manager import SkillCatalogEntry  # noqa: E402
from skillful_agent.tools import activate_skill  # noqa: E402


def _make_tool_context(
    skill_manager: object,
    active_skills: list[str] | None = None,
) -> MagicMock:
    """Build a minimal ToolContext mock with pre-populated state."""
    ctx = MagicMock()
    ctx.state = {
        "_skill_manager": skill_manager,
        "active_skills": active_skills if active_skills is not None else [],
    }
    return ctx


def _make_skill_manager(
    *,
    skill_name: str = "test-skill",
    mode: str = "inline",
    body: str = "# Skill body",
) -> MagicMock:
    """Build a SkillManager mock returning a single skill entry."""
    entry = SkillCatalogEntry(
        name=skill_name,
        description="A test skill.",
        location=Path("/fake/SKILL.md"),
        skill_dir=Path("/fake"),
        mode=mode,
    )
    manager = MagicMock()
    manager.load_skill_body.return_value = body
    manager.discover_skills.return_value = [entry]
    manager.available_names.return_value = [skill_name]
    manager.list_skill_resources.return_value = []
    return manager


@pytest.mark.anyio
async def test_activate_skill_inline_returns_skill_content() -> None:
    """activate_skill in inline mode must return a <skill_content> XML block."""
    manager = _make_skill_manager(skill_name="task-reminder", mode="inline")
    ctx = _make_tool_context(manager)

    result = await activate_skill("task-reminder", "ignored", ctx)

    assert result.startswith("<skill_content"), (
        f"Expected XML block starting with '<skill_content', got: {result[:80]!r}"
    )
    assert "task-reminder" in result


@pytest.mark.anyio
async def test_activate_skill_agent_mode_returns_subagent_text() -> None:
    """activate_skill in agent mode must return the sub-agent final text."""
    manager = _make_skill_manager(skill_name="code-agent", mode="agent")
    ctx = _make_tool_context(manager)

    with patch(
        "skillful_agent.tools._run_agent_skill",
        new=AsyncMock(return_value="sub-agent result"),
    ):
        result = await activate_skill("code-agent", "do something", ctx)

    assert result == "sub-agent result"
    # Agent-mode skills must NOT be marked active
    assert "code-agent" not in ctx.state.get("active_skills", [])


@pytest.mark.anyio
async def test_activate_skill_unknown_skill_returns_error() -> None:
    """activate_skill must return an error string when the skill is not found."""
    manager = MagicMock()
    manager.load_skill_body.return_value = None
    manager.available_names.return_value = []
    ctx = _make_tool_context(manager)

    result = await activate_skill("nonexistent-skill", "task", ctx)

    assert "not found" in result.lower()


@pytest.mark.anyio
async def test_activate_skill_inline_already_active() -> None:
    """Second call for an inline skill must return an 'already active' notice."""
    manager = _make_skill_manager(skill_name="task-reminder", mode="inline")
    ctx = _make_tool_context(manager, active_skills=["task-reminder"])

    result = await activate_skill("task-reminder", "any", ctx)

    assert "already active" in result.lower()
